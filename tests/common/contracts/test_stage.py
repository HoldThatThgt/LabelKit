from __future__ import annotations

import asyncio
import inspect
import random
from dataclasses import fields

from labelkit.common.contracts.stage import RunContext, Stage


def test_run_context_has_exactly_six_required_fields_in_contract_order():
    assert [field.name for field in fields(RunContext)] == [
        "cfg",
        "llm",
        "schema_engine",
        "metrics",
        "rng",
        "batch_no",
    ]
    assert all(
        parameter.default is inspect.Parameter.empty
        for parameter in inspect.signature(RunContext).parameters.values()
    )


def test_run_context_preserves_supplied_runtime_objects():
    cfg = object()
    llm = object()
    schema_engine = object()
    metrics = object()
    rng = random.Random(17)

    ctx = RunContext(cfg, llm, schema_engine, metrics, rng, 3)

    assert (ctx.cfg, ctx.llm, ctx.schema_engine, ctx.metrics) == (
        cfg,
        llm,
        schema_engine,
        metrics,
    )
    assert ctx.rng is rng
    assert ctx.batch_no == 3


def test_stage_protocol_freezes_name_and_async_run_signature():
    signature = inspect.signature(Stage.run)

    assert getattr(Stage, "_is_protocol", False) is True
    assert Stage.__annotations__ == {"name": "str"}
    assert inspect.iscoroutinefunction(Stage.run)
    assert list(signature.parameters) == ["self", "batch", "ctx"]
    assert signature.parameters["batch"].annotation == "list[PipelineItem]"
    assert signature.parameters["ctx"].annotation == "RunContext"
    assert signature.return_annotation == "list[PipelineItem]"


def test_stage_is_a_structural_async_contract_with_same_batch_return():
    class IdentityStage:
        name = "identity"

        async def run(self, batch, ctx):
            return batch

    stage: Stage = IdentityStage()
    batch = [object()]

    result = asyncio.run(stage.run(batch, object()))

    assert stage.name == "identity"
    assert result is batch
