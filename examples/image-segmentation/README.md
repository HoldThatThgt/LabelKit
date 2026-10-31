# 图片候选区域示例工程

segment 决定几何，annotate 只按 `region_id` 选择与标注，后处理钩子把几何写进输出。每个工程都读 `../config.toml`。

| 工程 | 输入 | 候选来源 | 输出 |
|---|---|---|---|
| `project.toml` | `data/photos`，只有图片 | 模型依据图片生成 | 保留多边形 |
| `project-boxes.toml` | `data/screens-xml`，截图加 uiautomator XML | 参考框直接成为候选，segment 不调用模型 | 外接矩形 |
| `project-uiautomator.toml` | `data/screens-xml` | 模型复用参考框或画新多边形，verify 开启 | 保留多边形 |
| `project-accessibility.toml` | `data/screens-json`，截图加无障碍树 JSON | 同上，classify 开启 | 保留多边形 |
| `project-ocr.toml` | `data/documents`，文档图片加 OCR JSON | 同上，框名由框架补为 `"1"…"N"` | 外接矩形 |
| `project-fixed.toml` | `data/fixed`，长边 2601 的大图 | 孔洞与多部件，请求缩放到 2048 | 保留多边形 |

`hooks.py` 给出 `uiautomator_to_boxes`、`accessibility_to_boxes`、`ocr_to_boxes` 三个 box_parser 钩子，以及 `keep_polygons`、`to_bounding_boxes` 两个后处理钩子。`schemas/` 里的 `region_id` 标了 `x-labelkit-region-ref: true`：模型引用不存在的候选时进入修复，不会走到后处理。

`data/` 与 `fixtures/` 都由 `prepare.py` 生成。`fixtures/fixed-candidates.json` 是 `project-fixed.toml` 的固定 segment 输出，其中的期望几何用精确有理数独立算出。通用模型不一定能画出孔洞与多部件，所以这条路径用夹具走离线测试，不代表模型能生成这样的候选。

无凭据的配置检查与 dry-run：

```bash
cd examples/image-segmentation
mkdir -p out
uv run labelkit validate --config ../config.toml --project project.toml --console plain
uv run labelkit run --config ../config.toml --project project.toml --dry-run --console plain
```

离线端到端测试在 `tests/operators/test_image_examples.py`，用进程内的模型桩跑真实阶段链。
