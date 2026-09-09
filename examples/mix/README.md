# 序列与帧双粒度示例

UI 主工程的 segment、序列与帧 classify、quality、序列与帧 annotate、verify 全部使用 `vision` profile，
保留每个成员的完整树和图片；文本姊妹工程继续使用 `default`。

每个输入会话独立暂存、统一质量比较并提交。`batch_size` 是计算组上限，输出序列可包含更多帧。
上下文不足时沿完整成员边界分区并重算未提交会话；不会抽图、截树或跨容量边界缝回去。
状态不跨进程重启保存。

从本目录执行不调用模型的配置校验：

```bash
uv run labelkit validate --config config.toml --project project.toml --console plain
uv run labelkit validate --config config.toml --project project-text.toml --console plain
```

手册保留的双端点调用数字与已有产物属于先前摘要实现的真实历史记录。当前 profile 分工已变化，
本次只完成配置校验，不用历史数字声称新运行通过。

[PENDING-EVIDENCE:mix-full-evidence-existing-example]

当前容量场景见 [会话容量示例](../sequence-context-capacity/README.md)。
