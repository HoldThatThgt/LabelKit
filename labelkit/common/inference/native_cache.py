"""为 Anthropic 原生请求设置推理强度和缓存边界。"""


def configure_native_body(body, options):
    """配置原生推理强度和可复用前缀，保持消息内容与顺序。"""
    body.pop("temperature", None)
    body["output_config"] = dict(options["output_config"])
    marker = dict(options["cache_control"])
    if body.get("system"):
        body["system"] = [{"type": "text", "text": body["system"], "cache_control": marker}]
    if body.get("tools"):
        body["tools"][-1]["cache_control"] = marker
    body["messages"][-1]["content"][-1]["cache_control"] = marker
    return body
