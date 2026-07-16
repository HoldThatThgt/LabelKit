#!/usr/bin/env python3
"""examples/thread 一次性 fixture 生成脚本（确定性——无随机、无时间戳）。

生成五个场景子目录共 47 对 uitree_N.jsonl / image_N.png 到
examples/thread/data/（`[stream] key = ["source_dir"]` 按子目录分会话；
index 命名空间全树唯一、各场景错开编号）：

  v1-serial/ 101–108        「串联」：任务 A 订机票 4 帧（101–104，
    package=com.example.flight）+ 任务 B 听音乐 4 帧（105–108，
    com.example.music）。预期 2 段 2 线索、零缝合零接缝。
  v2-single-cross/ 201–210  「单交叉」：任务 A 点外卖前半 4 帧（201–204，
    com.example.food）→ 任务 B 回复消息 3 帧（205–207，com.example.chat）
    → 任务 A 后半 3 帧（208–210）。实体延续：餐厅「蜀香园麻辣香锅」与
    「招牌麻辣香锅 ×1 ¥45」跨 203/204/208/209，订单号仅 210。预期 3 段
    缝成 2 线索（A=双碎片 1 接缝）。
  v3-multi-cross/ 301–315   「多交叉」：任务 A 订酒店 3+3+3 帧（301–303 /
    307–309 / 313–315，com.example.hotel）被任务 B 打车去商场 3 帧
    （304–306，com.example.taxi，目的地刻意与酒店行程无关）与任务 D
    记备忘 3 帧（310–312，com.example.notes）两度打断。实体延续：
    「湖畔云居酒店」跨 302–315 全部 A 帧，「大床房 ¥388」跨 308/309/313，
    订单号 HT20260802031 跨 314/315。预期 5 段缝成 3 线索（A=三碎片 2 接缝）。
  v4-noise-rescue/ 401–409  「噪声+救援」（dev-spec §1.1 规范布局
    x·A → y·B → 1 噪声帧 → w·A 尾段，w < min_len）：任务 A 网购跑步鞋
    4 帧（401–404，com.example.shop）→ 任务 B 刷新闻 3 帧（405–407，
    com.example.news）→ 408 低电量系统弹窗（com.example.powersave，
    非自愿插入，预期 dropped_noise）→ 409 支付成功尾帧（预期
    below_min_len 短段命中救援）。实体延续：「云驰跑步鞋 42码 ×1」跨
    403/404/409，订单号仅 409。预期 2 线索、rescued_short=1、1 接缝、
    1 噪声帧。
  neg-pure-noise/ 501–505   「纯噪声负样本」：锁屏 / 广告弹窗 / 系统更新 /
    误触相机 / 低电量弹窗各 1 帧，互不相关（package 各异）。预期零段
    零线索零缝合（负样本协议 E2）。

树是唯一语义源；节点行形态照抄 examples/ui/data/uitree_*.jsonl：
  {id, parent, class, text, bounds, visible, package[, content_desc]}
截图 = PIL 纯色底（每 App 一底色）+ 顶部标题大字 + 按树 bounds 画控件矩形
与文本，尽力而为——中文经系统字体（PingFang/STHeiti）渲染，字体加载失败
回退画 ASCII 替代文本；图片语义以树为准。PNG 为 8-bit RGB、pnginfo 置空，
重跑产物字节稳定。

用法：cd examples/thread && uv run python tools/gen_fixtures.py
"""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from PIL.PngImagePlugin import PngInfo

W, H = 400, 800
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

_FONT_CANDIDATES = (
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
)

# 每 App 一底色（含五个噪声场景 App）。
APPS = {
    "flight": {"package": "com.example.flight", "bg": (32, 82, 149),
               "title": (255, 255, 255), "on_bg": (255, 255, 255),
               "fill": (232, 240, 251), "outline": (17, 45, 84),
               "widget_text": (12, 36, 70)},
    "music": {"package": "com.example.music", "bg": (123, 82, 222),
              "title": (255, 255, 255), "on_bg": (255, 255, 255),
              "fill": (243, 238, 255), "outline": (74, 42, 150),
              "widget_text": (48, 28, 96)},
    "food": {"package": "com.example.food", "bg": (255, 138, 61),
             "title": (255, 255, 255), "on_bg": (255, 255, 255),
             "fill": (255, 244, 235), "outline": (191, 87, 17),
             "widget_text": (74, 40, 12)},
    "chat": {"package": "com.example.chat", "bg": (67, 160, 71),
             "title": (255, 255, 255), "on_bg": (255, 255, 255),
             "fill": (236, 248, 236), "outline": (27, 94, 32),
             "widget_text": (18, 60, 22)},
    "hotel": {"package": "com.example.hotel", "bg": (0, 137, 123),
              "title": (255, 255, 255), "on_bg": (255, 255, 255),
              "fill": (232, 247, 245), "outline": (0, 77, 64),
              "widget_text": (0, 51, 43)},
    "taxi": {"package": "com.example.taxi", "bg": (46, 124, 246),
             "title": (255, 255, 255), "on_bg": (255, 255, 255),
             "fill": (238, 245, 255), "outline": (18, 70, 160),
             "widget_text": (16, 42, 92)},
    "notes": {"package": "com.example.notes", "bg": (255, 179, 0),
              "title": (60, 40, 0), "on_bg": (60, 40, 0),
              "fill": (255, 248, 225), "outline": (161, 106, 0),
              "widget_text": (92, 58, 0)},
    "shop": {"package": "com.example.shop", "bg": (211, 47, 47),
             "title": (255, 255, 255), "on_bg": (255, 255, 255),
             "fill": (253, 236, 236), "outline": (139, 22, 22),
             "widget_text": (96, 14, 14)},
    "news": {"package": "com.example.news", "bg": (69, 90, 100),
             "title": (255, 255, 255), "on_bg": (245, 247, 248),
             "fill": (236, 241, 243), "outline": (38, 50, 56),
             "widget_text": (30, 42, 48)},
    "social": {"package": "com.example.social", "bg": (158, 158, 158),
               "title": (255, 255, 255), "on_bg": (250, 250, 250),
               "fill": (240, 240, 240), "outline": (105, 105, 105),
               "widget_text": (55, 55, 55)},
    "lockscreen": {"package": "com.example.lockscreen", "bg": (28, 34, 48),
                   "title": (240, 240, 245), "on_bg": (220, 222, 230),
                   "fill": (44, 52, 70), "outline": (90, 100, 125),
                   "widget_text": (225, 228, 235)},
    "adpop": {"package": "com.example.adpop", "bg": (233, 30, 99),
              "title": (255, 255, 255), "on_bg": (255, 255, 255),
              "fill": (253, 232, 240), "outline": (136, 14, 79),
              "widget_text": (100, 10, 58)},
    "sysupdate": {"package": "com.example.sysupdate", "bg": (96, 125, 139),
                  "title": (255, 255, 255), "on_bg": (245, 247, 248),
                  "fill": (236, 239, 241), "outline": (55, 71, 79),
                  "widget_text": (38, 50, 56)},
    "camera": {"package": "com.example.camera", "bg": (33, 33, 33),
               "title": (245, 245, 245), "on_bg": (230, 230, 230),
               "fill": (66, 66, 66), "outline": (130, 130, 130),
               "widget_text": (240, 240, 240)},
    "powermgr": {"package": "com.example.powermgr", "bg": (255, 111, 0),
                 "title": (255, 255, 255), "on_bg": (255, 255, 255),
                 "fill": (255, 243, 224), "outline": (150, 63, 0),
                 "widget_text": (102, 44, 0)},
    "powersave": {"package": "com.example.powersave", "bg": (121, 85, 72),
                  "title": (255, 255, 255), "on_bg": (255, 243, 236),
                  "fill": (247, 240, 236), "outline": (62, 39, 35),
                  "widget_text": (54, 34, 30)},
}

# 帧定义：(app, [(class, text, bounds[, content_desc]), ...])。
# 每帧 4–8 节点：FrameLayout 根 + TextView 标题（首个非空 text，即
# frame_digest 的 title）+ 若干 Button/EditText/TextView，全 visible，
# bounds 平铺于 400×800 内。同会话内不同任务的帧之间刻意避免任何完全
# 相同的文本串（缝合先验腿②为逐字实体重叠）；同一任务的碎片之间刻意
# 保留逐字延续实体。

# ── v1-serial：任务 A 订机票（101–104）+ 任务 B 听音乐（105–108） ──────────
V1_FRAMES: list[tuple[str, list[tuple]]] = [
    # 101 机票首页：出发地/目的地 + 搜索
    ("flight", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "机票查询", (24, 20, 200, 56)),
        ("TextView", "单程 · 7月20日", (24, 70, 220, 98)),
        ("EditText", "上海", (24, 110, 376, 158)),
        ("EditText", "成都", (24, 170, 376, 218)),
        ("Button", "搜索航班", (24, 700, 376, 760)),
    ]),
    # 102 航班列表：三个航班 Button
    ("flight", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "上海 → 成都", (24, 20, 260, 56)),
        ("TextView", "7月20日 单程", (24, 66, 200, 94)),
        ("Button", "MU5401 08:15 起飞 ¥760", (24, 110, 376, 180)),
        ("Button", "CA1946 12:30 起飞 ¥820", (24, 196, 376, 266)),
        ("Button", "HO1255 19:05 起飞 ¥690", (24, 282, 376, 352)),
    ]),
    # 103 订单填写：MU5401 + 乘机人 + 提交
    ("flight", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "订单填写", (24, 20, 180, 56)),
        ("TextView", "MU5401 上海虹桥 → 成都双流", (24, 80, 376, 116)),
        ("TextView", "乘机人 张伟", (24, 130, 220, 162)),
        ("TextView", "票价 ¥760", (24, 176, 180, 208)),
        ("Button", "提交订单", (24, 700, 376, 760)),
    ]),
    # 104 出票成功：订单号（任务 A 收尾）
    ("flight", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "出票成功", (24, 20, 180, 56)),
        ("TextView", "订单号 FL20260720066", (24, 90, 330, 126)),
        ("TextView", "MU5401 7月20日 08:15 起飞", (24, 140, 360, 172)),
        ("Button", "查看行程", (24, 700, 190, 760)),
        ("Button", "返回首页", (210, 700, 376, 760)),
    ]),
    # 105 音乐首页：推荐歌单（任务 B 开始，跨 App 无实体延续）
    ("music", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "云听音乐", (24, 20, 180, 56)),
        ("TextView", "推荐歌单", (24, 70, 160, 98)),
        ("Button", "夏日清凉", (24, 110, 186, 170)),
        ("Button", "驾车金曲", (214, 110, 376, 170)),
        ("EditText", "", (24, 700, 376, 748), "搜索歌曲"),
    ]),
    # 106 搜索：周杰伦
    ("music", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "搜索歌曲", (24, 20, 180, 56)),
        ("EditText", "周杰伦", (24, 70, 376, 118)),
        ("TextView", "晴天 - 周杰伦", (24, 140, 300, 176)),
        ("TextView", "七里香 - 周杰伦", (24, 188, 300, 224)),
        ("TextView", "稻香 - 周杰伦", (24, 236, 300, 272)),
    ]),
    # 107 播放页
    ("music", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "正在播放", (24, 20, 180, 56)),
        ("TextView", "晴天 - 周杰伦", (24, 90, 300, 126)),
        ("TextView", "00:42 / 04:29", (24, 140, 220, 172)),
        ("Button", "暂停", (24, 700, 190, 760)),
        ("Button", "下一首", (210, 700, 376, 760)),
    ]),
    # 108 已收藏（任务 B 收尾）
    ("music", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "已添加收藏", (24, 20, 220, 56)),
        ("TextView", "晴天 已加入「我的收藏」", (24, 90, 360, 126)),
        ("Button", "继续播放", (24, 700, 190, 760)),
        ("Button", "查看歌单", (210, 700, 376, 760)),
    ]),
]

# ── v2-single-cross：点外卖（201–204 / 208–210）× 回复消息（205–207） ──────
V2_FRAMES: list[tuple[str, list[tuple]]] = [
    # 201 外卖首页
    ("food", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "美食外卖", (24, 20, 200, 56)),
        ("EditText", "", (24, 80, 376, 128), "搜索美食"),
        ("TextView", "推荐餐厅", (24, 160, 160, 188)),
        ("TextView", "蜀香园麻辣香锅 4.9 分", (24, 200, 376, 240)),
        ("TextView", "老张牛肉面 4.6 分", (24, 248, 376, 288)),
    ]),
    # 202 搜索结果：两家香锅店
    ("food", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "麻辣香锅 搜索结果", (24, 20, 300, 56)),
        ("Button", "蜀香园麻辣香锅", (24, 80, 376, 150)),
        ("Button", "川渝香锅坊", (24, 166, 376, 236)),
        ("TextView", "共 2 家餐厅", (24, 254, 200, 282)),
    ]),
    # 203 店铺菜单：招牌麻辣香锅 ¥45（实体自此开始）
    ("food", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "蜀香园麻辣香锅", (24, 20, 260, 56)),
        ("TextView", "招牌麻辣香锅 ¥45", (24, 90, 320, 126)),
        ("TextView", "微辣 · 大份 · 含土豆宽粉", (24, 134, 340, 162)),
        ("TextView", "月售 800+ 好评率 97%", (24, 170, 300, 198)),
        ("Button", "加入购物车", (24, 700, 376, 760)),
    ]),
    # 204 购物车（A 前半尾帧：实体「蜀香园麻辣香锅」「招牌麻辣香锅 ×1 ¥45」）
    ("food", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "购物车", (24, 20, 150, 56)),
        ("TextView", "蜀香园麻辣香锅", (24, 90, 260, 122)),
        ("TextView", "招牌麻辣香锅 ×1 ¥45", (24, 130, 320, 166)),
        ("TextView", "合计 ¥45", (24, 180, 180, 212)),
        ("Button", "去结算", (24, 700, 376, 760)),
    ]),
    # 205 消息列表（任务 B：与 A 无实体交集）
    ("chat", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "消息", (24, 20, 120, 56)),
        ("TextView", "李经理", (24, 90, 160, 122)),
        ("TextView", "下午的会议改到 3 点了", (24, 130, 340, 162)),
        ("TextView", "项目组群聊 · 昨天", (24, 176, 280, 204)),
        ("Button", "打开对话", (24, 700, 376, 760)),
    ]),
    # 206 对话页：输入回复
    ("chat", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "李经理", (24, 20, 160, 56)),
        ("TextView", "下午的会议改到 3 点了，请准时参加", (24, 90, 376, 140)),
        ("EditText", "好的，3 点会议室见", (24, 640, 300, 688)),
        ("Button", "发送", (312, 640, 376, 688)),
    ]),
    # 207 已发送（任务 B 收尾）
    ("chat", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "李经理", (24, 20, 160, 56)),
        ("TextView", "好的，3 点会议室见", (120, 90, 376, 130)),
        ("TextView", "已送达", (300, 138, 376, 162)),
        ("TextView", "李经理: 收到", (24, 180, 240, 212)),
    ]),
    # 208 确认订单（A 后半首帧：逐字延续「蜀香园麻辣香锅」「招牌麻辣香锅 ×1 ¥45」）
    ("food", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "确认订单", (24, 20, 180, 56)),
        ("TextView", "蜀香园麻辣香锅", (24, 80, 260, 112)),
        ("TextView", "招牌麻辣香锅 ×1 ¥45", (24, 120, 320, 156)),
        ("TextView", "收货地址：静安区南京西路 88 号", (24, 170, 376, 222)),
        ("Button", "提交订单 ¥45", (24, 700, 376, 760)),
    ]),
    # 209 支付页
    ("food", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "支付订单", (24, 20, 180, 56)),
        ("TextView", "蜀香园麻辣香锅", (24, 90, 260, 122)),
        ("TextView", "应付 ¥45", (24, 130, 180, 162)),
        ("Button", "确认支付", (24, 700, 376, 760)),
    ]),
    # 210 下单成功：订单号（任务 A 收尾）
    ("food", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "下单成功", (24, 20, 180, 56)),
        ("TextView", "订单号 FD20260716552", (24, 90, 330, 126)),
        ("TextView", "蜀香园麻辣香锅 预计 40 分钟送达", (24, 140, 376, 176)),
        ("Button", "查看订单", (24, 700, 376, 760)),
    ]),
]

# ── v3-multi-cross：订酒店（301–303/307–309/313–315）× 打车（304–306）
#    × 记备忘（310–312） ─────────────────────────────────────────────────────
V3_FRAMES: list[tuple[str, list[tuple]]] = [
    # 301 酒店首页
    ("hotel", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "酒店预订", (24, 20, 180, 56)),
        ("EditText", "杭州", (24, 80, 376, 128)),
        ("TextView", "8月2日 入住 · 8月3日 离店", (24, 140, 340, 172)),
        ("Button", "搜索酒店", (24, 700, 376, 760)),
    ]),
    # 302 酒店列表（实体「湖畔云居酒店」自此开始）
    ("hotel", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "杭州酒店", (24, 20, 180, 56)),
        ("Button", "湖畔云居酒店 ¥388 起 4.8 分", (24, 80, 376, 150)),
        ("Button", "城东快捷酒店 ¥199 起 4.2 分", (24, 166, 376, 236)),
        ("Button", "西湖印象民宿 ¥520 起 4.9 分", (24, 252, 376, 322)),
    ]),
    # 303 酒店详情（A₁ 尾帧）
    ("hotel", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "湖畔云居酒店", (24, 20, 240, 56)),
        ("TextView", "大床房 ¥388/晚 含双早", (24, 90, 330, 126)),
        ("TextView", "免费取消 · 近西湖", (24, 134, 280, 166)),
        ("Button", "预订大床房", (24, 700, 376, 760)),
    ]),
    # 304 打车首页（任务 B：目的地为商场，与订酒店行程无关）
    ("taxi", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "快捷出行", (24, 20, 200, 56)),
        ("EditText", "城西银泰城", (24, 90, 376, 140)),
        ("TextView", "当前位置：文三路 199 号", (24, 150, 320, 182)),
        ("Button", "呼叫快车", (24, 700, 376, 760)),
    ]),
    # 305 选车型
    ("taxi", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "选择车型", (24, 20, 180, 56)),
        ("TextView", "目的地：城西银泰城", (24, 80, 300, 112)),
        ("Button", "经济型 ¥26", (24, 140, 376, 210)),
        ("Button", "舒适型 ¥38", (24, 226, 376, 296)),
    ]),
    # 306 司机接单（任务 B 收尾）
    ("taxi", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "司机已接单", (24, 20, 220, 56)),
        ("TextView", "车牌 浙A·88T66", (24, 90, 260, 126)),
        ("TextView", "预计 5 分钟到达", (24, 134, 260, 166)),
        ("Button", "联系司机", (24, 700, 376, 760)),
    ]),
    # 307 订单填写（A₂ 首帧：逐字延续「湖畔云居酒店」）
    ("hotel", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "订单填写", (24, 20, 180, 56)),
        ("TextView", "湖畔云居酒店", (24, 80, 240, 112)),
        ("TextView", "大床房 · 8月2日入住 共 1 晚", (24, 120, 350, 152)),
        ("EditText", "陈晨", (24, 170, 376, 218)),
        ("Button", "下一步", (24, 700, 376, 760)),
    ]),
    # 308 确认订单
    ("hotel", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "确认订单", (24, 20, 180, 56)),
        ("TextView", "湖畔云居酒店", (24, 80, 240, 112)),
        ("TextView", "大床房 ¥388", (24, 120, 220, 152)),
        ("TextView", "入住人 陈晨 138****6621", (24, 160, 320, 192)),
        ("Button", "去支付", (24, 700, 376, 760)),
    ]),
    # 309 支付页（A₂ 尾帧：「湖畔云居酒店」「大床房 ¥388」）
    ("hotel", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "支付订单", (24, 20, 180, 56)),
        ("TextView", "湖畔云居酒店", (24, 80, 240, 112)),
        ("TextView", "大床房 ¥388", (24, 120, 220, 152)),
        ("Button", "确认支付", (24, 700, 376, 760)),
    ]),
    # 310 备忘录列表（任务 D：与 A/B 无实体交集）
    ("notes", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "备忘录", (24, 20, 150, 56)),
        ("TextView", "周末大扫除", (24, 90, 240, 122)),
        ("TextView", "给绿萝浇水", (24, 130, 240, 162)),
        ("Button", "新建备忘", (24, 700, 376, 760)),
    ]),
    # 311 新建备忘
    ("notes", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "新建备忘", (24, 20, 180, 56)),
        ("EditText", "买牛奶、鸡蛋、面包", (24, 80, 376, 160)),
        ("Button", "保存", (24, 700, 376, 760)),
    ]),
    # 312 已保存（任务 D 收尾）
    ("notes", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "备忘录", (24, 20, 150, 56)),
        ("TextView", "买牛奶、鸡蛋、面包", (24, 90, 300, 122)),
        ("TextView", "周末大扫除", (24, 130, 240, 162)),
        ("TextView", "已保存", (300, 20, 376, 52)),
    ]),
    # 313 支付成功（A₃ 首帧：逐字延续「湖畔云居酒店」「大床房 ¥388」）
    ("hotel", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "支付成功", (24, 20, 180, 56)),
        ("TextView", "湖畔云居酒店", (24, 90, 240, 122)),
        ("TextView", "大床房 ¥388", (24, 130, 220, 162)),
        ("TextView", "已支付", (24, 170, 140, 202)),
        ("Button", "查看订单", (24, 700, 376, 760)),
    ]),
    # 314 订单详情：订单号
    ("hotel", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "订单详情", (24, 20, 180, 56)),
        ("TextView", "订单号 HT20260802031", (24, 90, 330, 126)),
        ("TextView", "湖畔云居酒店 8月2日入住", (24, 140, 340, 172)),
        ("TextView", "状态：已确认", (24, 180, 220, 212)),
        ("Button", "联系酒店", (24, 700, 376, 760)),
    ]),
    # 315 入住凭证（任务 A 收尾）
    ("hotel", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "入住凭证", (24, 20, 180, 56)),
        ("TextView", "湖畔云居酒店", (24, 90, 240, 122)),
        ("TextView", "订单号 HT20260802031", (24, 130, 330, 162)),
        ("TextView", "8月2日 14:00 后办理入住", (24, 170, 340, 202)),
    ]),
]

# ── v4-noise-rescue：网购（401–404 + 409 尾段）× 新闻（405–407）
#    + 408 噪声帧（§1.1 规范布局：尾段与其线索尾碎片之间隔着 B 与噪声帧） ────
V4_FRAMES: list[tuple[str, list[tuple]]] = [
    # 401 商城首页
    ("shop", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "潮品商城", (24, 20, 200, 56)),
        ("EditText", "", (24, 80, 376, 128), "搜索商品"),
        ("TextView", "今日热卖", (24, 160, 160, 188)),
        ("TextView", "云驰跑步鞋 ¥299", (24, 200, 300, 236)),
        ("TextView", "轻风运动短袖 ¥89", (24, 244, 300, 280)),
    ]),
    # 402 商品详情
    ("shop", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "云驰跑步鞋", (24, 20, 220, 56)),
        ("TextView", "¥299 · 42 码 黑白配色", (24, 90, 320, 126)),
        ("TextView", "月销 2000+ 好评率 98%", (24, 134, 300, 166)),
        ("Button", "立即购买", (24, 700, 376, 760)),
    ]),
    # 403 确认订单（实体「云驰跑步鞋 42码 ×1」自此开始）
    ("shop", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "确认订单", (24, 20, 180, 56)),
        ("TextView", "云驰跑步鞋 42码 ×1", (24, 80, 320, 116)),
        ("TextView", "应付 ¥299", (24, 124, 200, 156)),
        ("TextView", "收货地址：徐汇区漕溪北路 45 号", (24, 170, 376, 222)),
        ("Button", "提交订单", (24, 700, 376, 760)),
    ]),
    # 404 收银台（A 尾碎片尾帧：待支付即被打断）
    ("shop", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "收银台", (24, 20, 160, 56)),
        ("TextView", "支付订单 ¥299", (24, 90, 260, 126)),
        ("TextView", "云驰跑步鞋 42码 ×1", (24, 134, 320, 170)),
        ("Button", "立即支付", (24, 700, 376, 760)),
    ]),
    # 405 新闻首页（任务 B）
    ("news", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "每日头条", (24, 20, 180, 56)),
        ("TextView", "台风「木兰」明日登陆华南沿海", (24, 90, 376, 126)),
        ("TextView", "新能源车 6 月销量创新高", (24, 140, 360, 172)),
        ("TextView", "国产大飞机开通新航线", (24, 184, 340, 216)),
        ("Button", "查看更多", (24, 700, 376, 760)),
    ]),
    # 406 新闻详情
    ("news", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "台风「木兰」明日登陆", (24, 20, 340, 56)),
        ("TextView", "中央气象台发布橙色预警", (24, 90, 340, 122)),
        ("TextView", "华南多地中小学明日停课", (24, 130, 340, 162)),
        ("Button", "收藏", (24, 700, 190, 760)),
        ("Button", "分享", (210, 700, 376, 760)),
    ]),
    # 407 评论页（任务 B 收尾）
    ("news", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "评论 1284 条", (24, 20, 240, 56)),
        ("TextView", "希望大家注意安全", (24, 90, 300, 122)),
        ("TextView", "已经开始下雨了", (24, 130, 280, 162)),
        ("EditText", "", (24, 700, 376, 748), "写下你的评论"),
    ]),
    # 408 低电量系统弹窗（package 异域，非自愿插入）——预期 dropped_noise
    ("powersave", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "电量不足", (24, 20, 200, 56)),
        ("TextView", "剩余电量 18%，请及时充电", (24, 90, 350, 122)),
        ("Button", "开启省电模式", (24, 700, 240, 760)),
        ("Button", "知道了", (260, 700, 376, 760)),
    ]),
    # 409 支付成功尾帧（w=1 < min_len=2 ⇒ below_min_len ⇒ 救援候选；
    #     逐字延续「云驰跑步鞋 42码 ×1」，订单号仅本帧）
    ("shop", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "支付成功", (24, 20, 180, 56)),
        ("TextView", "云驰跑步鞋 42码 ×1", (24, 90, 320, 126)),
        ("TextView", "实付 ¥299", (24, 134, 200, 166)),
        ("TextView", "订单号 SP20260716477", (24, 174, 330, 206)),
        ("Button", "查看订单", (24, 700, 376, 760)),
    ]),
]

# ── neg-pure-noise：五帧互不相关的插入屏（负样本协议 E2：零线索零缝合） ────
NEG_FRAMES: list[tuple[str, list[tuple]]] = [
    # 501 锁屏
    ("lockscreen", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "14:32", (24, 20, 200, 76)),
        ("TextView", "7月16日 星期四", (24, 90, 260, 122)),
        ("TextView", "上滑解锁", (140, 700, 260, 732)),
    ]),
    # 502 广告弹窗
    ("adpop", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "限时优惠", (24, 20, 200, 56)),
        ("TextView", "新人专享大礼包", (24, 90, 280, 126)),
        ("Button", "立即领取", (24, 700, 190, 760)),
        ("Button", "关闭", (210, 700, 376, 760)),
    ]),
    # 503 系统更新弹窗
    ("sysupdate", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "系统更新", (24, 20, 200, 56)),
        ("TextView", "正在下载更新包 37%", (24, 90, 300, 126)),
        ("Button", "暂停下载", (24, 700, 376, 760)),
    ]),
    # 504 误触相机
    ("camera", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "相机", (24, 20, 140, 56)),
        ("TextView", "轻触屏幕对焦", (24, 90, 240, 122)),
        ("Button", "拍照", (150, 700, 250, 760)),
    ]),
    # 505 低电量弹窗
    ("powermgr", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "电量不足", (24, 20, 200, 56)),
        ("TextView", "剩余电量 15%", (24, 90, 240, 122)),
        ("Button", "开启省电模式", (24, 700, 240, 760)),
        ("Button", "取消", (260, 700, 376, 760)),
    ]),
]

# 场景表：(子目录, 起始 index, 帧表)。index 全树唯一、各场景错开编号。
SCENARIOS: list[tuple[str, int, list[tuple[str, list[tuple]]]]] = [
    ("v1-serial", 101, V1_FRAMES),
    ("v2-single-cross", 201, V2_FRAMES),
    ("v3-multi-cross", 301, V3_FRAMES),
    ("v4-noise-rescue", 401, V4_FRAMES),
    ("neg-pure-noise", 501, NEG_FRAMES),
]

TITLE_SIZE = 30
BODY_SIZE = 18
PLACEHOLDER_COLOR = (150, 150, 150)


def _load_fonts() -> tuple[dict[int, ImageFont.FreeTypeFont] | None, bool]:
    """Try system CJK fonts; on total failure fall back to the PIL bitmap
    font (ASCII-substitute text is drawn instead — tree stays authoritative)."""
    for path in _FONT_CANDIDATES:
        try:
            fonts = {size: ImageFont.truetype(path, size=size)
                     for size in (TITLE_SIZE, 18, 16, 14, 12)}
            return fonts, True
        except OSError:
            continue
    return None, False


_FONTS, CJK_OK = _load_fonts()
_BITMAP_FONT = None if CJK_OK else ImageFont.load_default()


def _ascii_fallback(text: str, role: str) -> str:
    kept = text.encode("ascii", "ignore").decode().strip()
    return kept or role


def _font_for(size: int):
    return _FONTS[size] if CJK_OK else _BITMAP_FONT


def _fit_size(draw: ImageDraw.ImageDraw, text: str, box_w: int, start: int) -> int:
    """Largest candidate size whose rendered width fits box_w (best effort —
    the smallest size is returned even if it still overflows)."""
    candidates = [s for s in (start, 18, 16, 14, 12) if s <= start]
    for size in candidates:
        bbox = draw.textbbox((0, 0), text, font=_font_for(size))
        if bbox[2] - bbox[0] <= box_w:
            return size
    return candidates[-1]


def _draw_text(draw: ImageDraw.ImageDraw, text: str,
               bounds: tuple[int, int, int, int], size: int, color,
               center: bool) -> None:
    if not text:
        return
    l, t, r, b = bounds
    pad = 8
    size = _fit_size(draw, text, r - l - 2 * pad, size)
    font = _font_for(size)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if center:
        xy = (l + max((r - l - tw) // 2, pad), t + max((b - t - th) // 2, 2))
    else:
        xy = (l + pad, t + max((b - t - th) // 2, 2))
    draw.text(xy, text, font=font, fill=color)


def render_image(app: str, nodes: list[tuple], path: Path) -> None:
    style = APPS[app]
    img = Image.new("RGB", (W, H), style["bg"])
    draw = ImageDraw.Draw(img)
    title_seen = False
    for i, spec in enumerate(nodes):
        if i == 0:                                    # root = 纯色底
            continue
        role, text, bounds = spec[0], spec[1], spec[2]
        content_desc = spec[3] if len(spec) > 3 else ""
        shown = text if CJK_OK else _ascii_fallback(text, role)
        if not title_seen and text:                   # 首个非空 text = 标题大字
            title_seen = True
            _draw_text(draw, shown, bounds, TITLE_SIZE, style["title"],
                       center=False)
            continue
        if role == "Button":
            draw.rounded_rectangle(list(bounds), radius=10,
                                   fill=style["fill"],
                                   outline=style["outline"], width=2)
            _draw_text(draw, shown, bounds, BODY_SIZE,
                       style["widget_text"], center=True)
        elif role == "EditText":
            draw.rounded_rectangle(list(bounds), radius=6,
                                   fill=(255, 255, 255),
                                   outline=style["outline"], width=2)
            if text:
                _draw_text(draw, shown, bounds, BODY_SIZE,
                           style["widget_text"], center=False)
            elif content_desc:                        # 空输入框画灰色占位文本
                placeholder = (content_desc if CJK_OK
                               else _ascii_fallback(content_desc, role))
                _draw_text(draw, placeholder, bounds, BODY_SIZE,
                           PLACEHOLDER_COLOR, center=False)
        else:                                         # TextView 等：纯文本
            _draw_text(draw, shown, bounds, BODY_SIZE, style["on_bg"],
                       center=False)
    # pnginfo 置空：不写任何辅助块，保证重跑字节稳定。
    img.save(path, format="PNG", pnginfo=PngInfo())


def build_tree(app: str, nodes: list[tuple]) -> str:
    pkg = APPS[app]["package"]
    lines = []
    for i, spec in enumerate(nodes):
        role, text, bounds = spec[0], spec[1], spec[2]
        obj: dict = {
            "id": str(i),
            "parent": None if i == 0 else "0",
            "class": role,
            "text": text,
            "bounds": list(bounds),
            "visible": True,
            "package": pkg,
        }
        if len(spec) > 3:
            obj["content_desc"] = spec[3]
        lines.append(json.dumps(obj, ensure_ascii=False))
    return "\n".join(lines) + "\n"


def main() -> None:
    if not CJK_OK:
        print("warning: 未找到系统中文字体，截图文字回退为 ASCII 替代"
              "（树语义不受影响）")
    total = 0
    for dirname, start, frames in SCENARIOS:
        scenario_dir = DATA_DIR / dirname
        scenario_dir.mkdir(parents=True, exist_ok=True)
        for offset, (app, nodes) in enumerate(frames):
            index = start + offset
            tree_path = scenario_dir / f"uitree_{index}.jsonl"
            image_path = scenario_dir / f"image_{index}.png"
            tree_path.write_text(build_tree(app, nodes), encoding="utf-8")
            render_image(app, nodes, image_path)
            print(f"wrote {dirname}/uitree_{index}.jsonl + image_{index}.png "
                  f"({APPS[app]['package']})")
            total += 1
    print(f"done: {total} pairs in {DATA_DIR}")


if __name__ == "__main__":
    main()
