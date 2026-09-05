# 标注后处理教学工程

这个工程把语义判断留给模型，把可以由输入机械复核的字段交给同步 Python 函数。完整 Schema 仍然约束
最终交付对象，带 `x-labelkit-postprocessor: true` 的 property 不会发送给模型。

普通工程识别中文文本中的车牌。模型只返回 `entities[].value`；`hooks.py` 规范化值，并从真实
`record.text` 计算 Unicode code point 的排他 `start`、`end` 和 `entity_count`。输入只含 `case_id` 与
`text`，预期实体位于独立的 `oracles/plates.json`。`check_output.py` 不导入钩子，而是按 `case_id` 连接
oracle，以实体集合和原文切片检查最终结果。

sequence 工程复用相邻 `sequence-generation` 工程已经验证的订票状态 Schema、catalog 与状态校验函数。
它只交付一个三事件成功正例，并派生一个三事件 replay。序列钩子在 `record is None` 的边界计算
`summary_length`；帧钩子先核对模型字段与真实成员 `record.payload`，再计算摘要、request_id 与 utterance
的 Unicode 长度。独立检查器同时核对 main、primary、replay、成员视图、
完整 Schema、report 计数、交付摘要和 manifest 文件哈希。

无凭据的配置检查和 dry-run：

```bash
cd examples/annotation-postprocessing
mkdir -p out
uv run labelkit validate --config config-local-4b.toml --project project.toml --console plain
uv run labelkit run --config config-local-4b.toml --project project.toml --dry-run --console plain
uv run labelkit validate --config config-local-4b.toml --project project-sequence.toml --console plain
uv run labelkit run --config config-local-4b.toml --project project-sequence.toml --dry-run --console plain
```

端口 `18081` 上已有真实 `Qwen3.5-4B-Q6_K.gguf` Anthropic 兼容服务时，运行并独立验收普通工程：

```bash
export LABELKIT_LOCAL_KEY='<local-service-key>'
uv run labelkit run --config config-local-4b.toml --project project.toml --console plain
uv run python check_output.py
```

运行并独立验收带帧标注和 replay 的 sequence 工程：

```bash
uv run labelkit run --config config-local-4b.toml --project project-sequence.toml --console plain
uv run python check_output.py --sequence
```

仓库真实集成门连续运行两轮普通工程和一轮 sequence 工程。它透明观察真实请求序列化结果以确认模型
Schema 与提示词没有代码负责字段，不替换模型服务、transport 或响应：

```bash
uv run --python 3.12 pytest tests/integration/test_postprocessing_local_llm.py -q -s \
  -m 'integration and local_llm'
```
