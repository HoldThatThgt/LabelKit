"""按 TeamoRouter 的 Anthropic 原生协议设置缓存边界、记录网关用量。"""
import json
import os
from pathlib import Path

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

def record_native_response(response, body):
    """保存原生 HTTP 响应的缓存账单，不记录密钥、图像或提示词正文。"""
    request = response.request
    data = {}
    if "json" in response.headers.get("content-type", ""):
        data = response.json()
    stats = data.get("usage") or {}
    user = body["messages"][-1]["content"]
    receipt = {"method": request.method, "url": str(request.url), "http_status": response.status_code,
               "stream_complete": response.extensions.get("native_message_stop", False),
               "api_version": request.headers.get("anthropic-version"), "has_api_key": "x-api-key" in request.headers,
               "request_model": body["model"], "response_model": data.get("model"), "response_id": data.get("id"),
               "thinking": body.get("thinking"), "effort": body.get("output_config", {}).get("effort"),
               "system_cache": body["system"][-1].get("cache_control"),
               "user_cache": user[-1].get("cache_control"), "usage": {k: stats.get(k) for k in
                   ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")}}
    with Path(os.environ["LABELKIT_NATIVE_RECEIPTS"]).open("a", encoding="utf8") as stream:
        stream.write(json.dumps(receipt,ensure_ascii=False)+"\n")
