"""用 Anthropic SDK 消费 SSE，向 LabelKit 返回完整的原生消息。"""
import httpx
from anthropic import APIConnectionError, APIError, APIStatusError, AsyncAnthropic

async def post_native_stream(http, url, body, headers, timeout):
    """读取至 message_stop，将 SDK 汇总的完整消息交给现有 Schema 和缓存记账。"""
    client = AsyncAnthropic(http_client=http, api_key=headers["x-api-key"],
                            base_url=url.removesuffix("/v1/messages"), timeout=timeout, max_retries=0)
    try:
        async with client.messages.stream(**body, extra_headers=headers) as stream:
            stopped = False
            async for event in stream:
                if event.type == "message_stop":
                    stopped = True
            if not stopped:
                raise httpx.RemoteProtocolError("Anthropic stream ended before message_stop")
            result = await stream.get_final_message()
            return httpx.Response(stream.response.status_code, json=result.model_dump(mode="json",exclude_none=True),
                                  request=stream.response.request)
    except APIStatusError as error:
        await error.response.aread()
        return error.response
    except (APIConnectionError, APIError) as error:
        raise httpx.RemoteProtocolError(f"Anthropic stream failed: {type(error).__name__}: {error}") from error
