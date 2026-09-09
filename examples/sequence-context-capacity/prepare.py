"""生成无秘密的确定性本地门禁输入；只准备文件，不访问模型。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent


def write_json(path, value):
    """保存可直接审阅的测试工件。"""
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def project_text(case, modality, instruction, *, strategy="rules", stitch=False):
    """生成显式、独立项目，容量注入不改正常配置。"""
    input_path = f"data/{case}" if modality == "ui" else f"data/{case}.jsonl"
    order = "input_order" if modality == "ui" else "meta:ts"
    gap = "gap_s = 900" if modality == "text" else ""
    stitch_options = 'llm = "default"\nrepass = true' if stitch else ""
    return f'''schema_version = 1
[run]
input = "{input_path}"
output = "out/{case}.jsonl"
modality = "{modality}"
batch_size = 2
seed = 42
[input]
ui_tree_max_chars = 30
[stream]
order_by = "{order}"
{gap}
session_max_len = 64
[segment]
enabled = true
strategy = "{strategy}"
window = 32
min_len = 2
on_error = "fail"
noise_filter = {str(strategy != "rules").lower()}
context = "仓库配货和聊天是独立任务；切换应用、再回仓库都要建立新片段。仓库同一请求的修改和确认属于同一任务；色卡连续检查也是单一任务。"
[stitch]
enabled = {str(stitch).lower()}
{stitch_options}
[dedup]
enabled = false
[quality]
enabled = false
[annotate]
enabled = true
instruction = {json.dumps(instruction, ensure_ascii=False)}
[verify]
enabled = true
llm = "default"
policy = "repair"
max_repair_rounds = 1
extra_criteria = "容量切分的人工边界不是任务内容；摘要只需覆盖当前完整成员中的事实。测试色卡画面和界面树是不同的完整证据。"
[trace]
enabled = true
channels = ["segment", "stitch", "annotate", "verify", "schema"]
content = "refs"
[output]
meta_mode = "inline"
rejects = "full"
max_repair_attempts = 1
schema_path = "schemas/{case}.json"
'''


def ui_frame(directory, ordinal, app, text, color):
    """生成完整小图及独立可见树；颜色只在像素中，不写入树或文件名。"""
    directory.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (256, 256), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=18)
    draw.text((14, 12), "WAREHOUSE" if app == "warehouse" else "MESSAGES", fill="black", font=font)
    draw.text((14, 42), f"STEP {ordinal}", fill="black", font=font)
    draw.rectangle((35, 85, 220, 225), fill=color)
    if color == "#dddddd":
        body_font = ImageFont.truetype("/System/Library/Fonts/STHeiti Medium.ttc", 17)
        body = "\n".join(text[index:index + 13] for index in range(0, len(text), 13))
        draw.rectangle((4, 74, 252, 250), fill="white")
        draw.multiline_text((10, 78), body, fill="black", font=body_font, spacing=3)
    image.save(directory / f"image_{ordinal}.png")
    nodes = [
        {"id": "0", "parent": None, "class": "FrameLayout", "text": "", "visible": True,
         "bounds": [0, 0, 256, 256], "package": f"com.example.{app}"},
        {"id": "1", "parent": "0", "class": "TextView", "text": text, "visible": True,
         "bounds": [0, 74, 256, 256] if color == "#dddddd" else [0, 0, 256, 70],
         "package": f"com.example.{app}"},
    ]
    (directory / f"uitree_{ordinal}.jsonl").write_text(
        "\n".join(json.dumps(node, ensure_ascii=False) for node in nodes) + "\n", encoding="utf-8")


def prepare_ui(root):
    """二十五帧全图门禁，唯一异色帧位于中间，树不披露颜色。"""
    for ordinal in range(1, 26):
        ui_frame(root / "data" / "ui", ordinal, "warehouse",
                 f"AUDIT-25 inventory card inspection step {ordinal}. Full visible evidence tail KEEP-TREE-{ordinal}.",
                 "#00aa00" if ordinal == 13 else "#0000dd")
    schema = {"type": "object", "properties": {"marker_color": {"type": "string"},
              "frame_count": {"type": "integer"}}, "required": ["marker_color", "frame_count"],
              "additionalProperties": False}
    write_json(root / "schemas" / "ui.json", schema)
    instruction = "依次检查全部截图，每张中央都有色卡。只有一帧颜色不同，marker_color填这张异色色卡的英文颜色名；frame_count填本序列实际截图总数。不可根据控件树猜颜色。"
    (root / "project-ui.toml").write_text(project_text("ui", "ui", instruction), encoding="utf-8")


def prepare_stitch(root):
    """仓库任务被聊天打断后恢复，实体、应用及回归页面提供真实缝合证据。"""
    messages = [
        ("warehouse", "RIVER-42 配货单详情：西仓，红色零件3箱。"),
        ("warehouse", "RIVER-42 配货单详情：修改颜色为蓝色，保持西仓3箱。"),
        ("warehouse", "RIVER-42 配货单详情：蓝色零件3箱，稍后确认发货。"),
        ("messages", "与同事聊天：今晚去公园散步，时间为19点。"),
        ("messages", "与同事聊天：确认19点公园见，与仓库配货无关。"),
        ("warehouse", "RIVER-42 配货单详情：恢复刚才的配货任务，原蓝色零件3箱。"),
        ("warehouse", "RIVER-42 配货单详情：最终目的仓改为东仓，数量改为7箱。"),
        ("warehouse", "RIVER-42 配货单详情：最终蓝色零件7箱发往东仓，发货完成。"),
    ]
    for ordinal, (app, text) in enumerate(messages, 1):
        ui_frame(root / "data" / "stitch", ordinal, app, text, "#dddddd")
    schema = {"type": "object", "properties": {"task": {"type": "string"}, "request_id": {"type": "string"},
              "destination": {"type": "string"}, "color": {"type": "string"}, "quantity": {"type": "integer"}},
              "required": ["task", "request_id", "destination", "color", "quantity"], "additionalProperties": False}
    write_json(root / "schemas" / "stitch.json", schema)
    instruction = "完整阅读此任务成员。仓库任务task填dispatch，提取最终request_id、destination、color、quantity。聊天任务task填chat，其余三个字符串填空串，quantity填0。"
    project = project_text("stitch", "ui", instruction, strategy="hybrid", stitch=True)
    original = "容量切分的人工边界不是任务内容；摘要只需覆盖当前完整成员中的事实。测试色卡画面和界面树是不同的完整证据。"
    criteria = "只依据给出的实际成员审查，不假设未观察到的未来状态。仓库最后一个成员已明确发货完成，因此其确认状态就是最终状态。聊天任务按标注指令使用task=chat、三个空字符串和quantity=0，这是有效目标格式。仓库任务中途被聊天打断又回到同一请求时，可以形成多个碎片；只因时间不连续不能判wrong_stitch，须指出请求或任务身份不同的具体证据。"
    project = project.replace(original, criteria)
    (root / "project-stitch.toml").write_text(project, encoding="utf-8")


def prepare_capacity(root, case):
    """用公开固定重复内容制造静态压力或真实服务窗口不符，不裁剪任何成员。"""
    if case == "static":
        payloads = ["容量证据" * 650 for _ in range(6)]
        window = 8192
    elif case == "reactive":
        payloads = [" x" * 18000 for _ in range(2)]
        window = 65536
    else:
        payloads = ["确认纸箱包装", "确认发货", " x" * 34000]
        window = 65536
    rows = [{"ts": f"2026-09-09T{'10' if case == 'minimum' and index == 2 else '09'}:00:{index:02d}+08:00",
             "text": f"继续处理同一个请求 CAPACITY。完整附录如下：{payload}。附录结束，确认请求 CAPACITY。"}
            for index, payload in enumerate(payloads)]
    (root / "data" / f"{case}.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    write_json(root / "schemas" / f"{case}.json", {"type": "object", "properties": {"request_id": {"type": "string"}},
               "required": ["request_id"], "additionalProperties": False})
    project = project_text(case, "text", "提取完整成员中反复确认的请求编号到request_id。")
    (root / f"project-{case}.toml").write_text(project, encoding="utf-8")
    config = (root / "config-local-4b.toml").read_text(encoding="utf-8")
    config = config.replace("context_window = 32768", f"context_window = {window}")
    config = config.replace("max_output_tokens = 2048", "max_output_tokens = 512")
    (root / f"config-{case}.toml").write_text(config, encoding="utf-8")


def prepare(root, case):
    """准备单一用例的可复现文件集。"""
    if case == "ui":
        prepare_ui(root)
    elif case == "stitch":
        prepare_stitch(root)
    else:
        prepare_capacity(root, case)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("case", choices=("ui", "stitch", "static", "reactive", "minimum"))
    args = parser.parse_args()
    prepare(ROOT, args.case)
    print(f"Prepared {args.case}; no model request was sent")
