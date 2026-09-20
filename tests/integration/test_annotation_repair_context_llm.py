"""通过真实 z.ai 验证普通记录的标注修复实际发送原始上下文。"""
from __future__ import annotations

import json
import random
from dataclasses import replace

import pytest

from labelkit.common.contracts.stage import RunContext
from labelkit.common.contracts.types import PipelineItem
from labelkit.common.inference.credentials import resolve_credentials
from labelkit.common.inference.schema_engine import SchemaEngine
from labelkit.operators.annotate import AnnotateStage
from tests.integration.test_annotate_llm import (
    _DirectTasks,
    _RecordingMetrics,
    make_cfg,
    text_record,
)
from tests.llm_client_helpers import make_llm_client

pytestmark = [pytest.mark.integration, pytest.mark.zai]


async def test_process_annotation_repair_sends_original_context_to_real_endpoint():
    """首个合法候选触发一次真实 L3，观察实际 HTTP 正文而不替换响应。"""
    cfg = make_cfg()
    cfg = replace(cfg, output=replace(cfg.output, max_repair_attempts=1), model_user_schema=cfg.user_schema,
                  llm_profiles={"default": replace(cfg.llm_profiles["default"], supports_structured_output=True)})
    source = "订单 R8Q2 的目的地是青岛，请帮我写一句确认信息。"
    inspected = []
    requests = []

    def require_second_validation(output, record):
        inspected.append(output)
        return ["请依据原始数据再次核对所有字段。"] if len(inspected) == 1 else []

    async def observe_request(request):
        requests.append(json.loads(request.content))

    llm = make_llm_client(cfg.llm_profiles, cfg.embedding_profiles, resolve_credentials(cfg))
    llm._http().event_hooks["request"].append(observe_request)
    engine = SchemaEngine(dict(cfg.user_schema), llm, cfg.output, validator=require_second_validation)
    ctx = RunContext(cfg=cfg, llm=llm, schema_engine=engine, rng=random.Random(0), batch_no=1,
                     metrics=_RecordingMetrics(), tasks=_DirectTasks(), task_namespace="integration:repair-context")
    item = PipelineItem(record=text_record("abcdef0123456789", source))
    try:
        await AnnotateStage(cfg).run([item], ctx)
    finally:
        await llm.aclose()

    assert item.status == "active" and item.annotation is not None, {
        "errors": [(error.kind, error.message) for error in item.errors],
        "requests": len(requests), "validated_candidates": len(inspected),
    }
    assert item.annotation.attempts == 2 and len(inspected) == 2
    assert item.annotation.usage.prompt_tokens > 0 and item.annotation.usage.completion_tokens > 0
    assert len(requests) == 2
    assert "writing_assist" in requests[0]["system"]
    assert source in json.dumps(requests[0]["messages"], ensure_ascii=False)
    assert source in json.dumps(requests[1]["messages"], ensure_ascii=False)
    assert requests[1]["system"] == requests[0]["system"]
    assert requests[1]["messages"][:len(requests[0]["messages"])] == requests[0]["messages"]
    assert engine.stats["l3_1"] == 1
