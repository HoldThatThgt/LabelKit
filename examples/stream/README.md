# 普通流示例

输入是固定文件组。每个语义会话在同次运行内完整处理，`batch_size` 只限制计算组的叶任务数量；
它不会按帧数切断会话。缝合可以跨计算组，不能跨语义会话或人工容量封闭边界。

下游使用完整成员文本、可见 UI 树与图片。容量不足时在会话提交前沿完整成员边界拆分并重算；
原始后段保留，sealed 序列禁止重新并入。状态只在当前进程内存中，进程重启不会恢复。

先验证配置，以下命令不调用模型：

```bash
uv run labelkit validate --config ../config.toml --project project.toml --console plain
uv run labelkit validate --config ../config.toml --project project-text.toml --console plain
```

手册第 25、26 章和已有输出中的数字是容量改造前的历史运行证据，不能作为新配置的预期调用数或质量值。
本次仅完成配置校验；更新后的完整真实模型运行证据尚未采集。

[PENDING-EVIDENCE:stream-full-session-existing-example]

独立容量场景与验证入口见 [会话容量示例](../sequence-context-capacity/README.md)。
