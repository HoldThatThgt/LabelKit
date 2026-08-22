"""M6 平面生成 Stage（spec 3.6，CONTRACTS §7.5）。"""
from __future__ import annotations

import asyncio
import logging
import math
from typing import TYPE_CHECKING, Sequence

from labelkit.common.contracts.types import Classification, PipelineItem, Record
from labelkit.common.errors import CircuitBreakerTripped, ContextOverflowError, InternalError, LabelKitError
from labelkit.common.runtime.schema_engine import CallScope
from labelkit.operators.generation.flat import (
    CallPlan,
    ClassSegment,
    SimilarityFilter,
    bucket_key,
    build_call_plans,
    build_class_segments,
    build_generate_prompt,
    build_segment_plans,
    canonical_json,
    effective_generate,
    error_kind as _error_kind,
    fit_plan_seeds as _fit_plan_seeds,
    make_generated_record,
    postprocess_samples,
    predraw_llm_style,
    render_prompt_texts,
    samples_schema as _samples_schema,
    select_seeds,
    void_log_message,
)

if TYPE_CHECKING:
    from labelkit.common.config.model import ResolvedConfig
    from labelkit.common.contracts.stage import RunContext


_log = logging.getLogger("labelkit.generate")


class GenerateStage:
    """只运行既有 flat/process 生成路径的 M6 Stage。"""

    name = "generate"

    def __init__(self, cfg: "ResolvedConfig"):
        """构造平面生成 Stage。

        @param cfg 已解析配置。
        """
        if cfg.generate.form == "sequence":
            _log.error("sequence generation cannot enter GenerateStage")
            raise InternalError("sequence generation cannot enter GenerateStage")
        self._cfg = cfg

    async def run(
        self,
        batch: list[PipelineItem],
        ctx: "RunContext",
    ) -> list[PipelineItem]:
        """从 process 批次种子返回新平面记录子批。

        @param batch 当前批，只读其活动质量幸存者。
        @param ctx 运行上下文。
        @return 新 PipelineItem 子批。
        """
        pools = select_seeds(batch, self._cfg)
        if not pools:
            return []
        segments = build_class_segments(pools, self._cfg)
        records = await self._generate(segments, ctx, None)
        return [self._make_item(record, class_name) for record, class_name in records]

    async def generate_all(self, ctx: "RunContext") -> list[Record]:
        """生成 generate_only flat 形态的全部记录。

        @param ctx 运行上下文。
        @return 按预抽调用序排列的记录。
        """
        generate = self._cfg.generate
        if generate.seed_examples:
            seeds: list[tuple[str | None, str]] = [
                (None, text) for text in generate.seed_examples
            ]
            num_calls = math.ceil(
                len(seeds) * generate.num_per_record / generate.num_per_call
            )
        else:
            seeds = []
            num_calls = math.ceil((generate.standalone_count or 0) / generate.num_per_call)
        segment = ClassSegment(None, tuple(seeds), num_calls, generate.styles)
        records = await self._generate([segment], ctx, self._cfg.limit)
        return [record for record, _ in records]

    async def _generate(
        self,
        segments: Sequence[ClassSegment],
        ctx: "RunContext",
        limit: int | None,
    ) -> list[tuple[Record, str | None]]:
        """执行平面生成的计划、派发与后处理主干。

        @param segments 已排序的类段。
        @param ctx 运行上下文。
        @param limit 记录上限或 None。
        @return 记录与 owning 类名对。
        """
        generate = self._cfg.generate
        num_calls = sum(segment.num_calls for segment in segments)
        exec_calls = num_calls
        if limit is not None:
            exec_calls = min(num_calls, math.ceil(limit / generate.num_per_call))
        plans = build_segment_plans(generate, segments, ctx.rng, exec_calls)
        schema = _samples_schema(generate.num_per_call)
        fitted = self._fit_plans(plans, ctx)
        results = await asyncio.gather(
            *(self._one_generate_call(plan, unfittable, schema, ctx)
              for plan, unfittable in fitted)
        )
        sent_plans = [plan for plan, _ in fitted]
        seed_texts = [text for segment in segments for _, text in segment.seeds]
        records = postprocess_samples(sent_plans, results, seed_texts, self._cfg, ctx.metrics)
        return records if limit is None else records[:limit]

    def _fit_plans(
        self,
        plans: Sequence[CallPlan],
        ctx: "RunContext",
    ) -> list[tuple[CallPlan, bool]]:
        """按目标 profile 预算装填全部调用计划。

        @param plans 预抽调用计划。
        @param ctx 运行上下文。
        @return 装填计划与不可装填标志。
        """
        fitted: list[tuple[CallPlan, bool]] = []
        for plan in plans:
            candidate, truncated, unfittable = _fit_plan_seeds(plan, self._cfg)
            if truncated:
                ctx.metrics.count("budget.truncations.generate")
            fitted.append((candidate, unfittable))
        return fitted

    async def _one_generate_call(
        self,
        plan: CallPlan,
        unfittable: bool,
        schema: dict,
        ctx: "RunContext",
    ) -> list[str] | None:
        """派发一次平面生成调用。

        @param plan 装填后的调用计划。
        @param unfittable 是否在一条种子处仍超预算。
        @param schema 本轮共用的 samples Schema。
        @param ctx 运行上下文。
        @return 样本文本或 None。
        """
        if unfittable:
            self._log_unfittable(plan, ctx)
            return None
        generate = effective_generate(self._cfg, plan.class_name)
        prompt = build_generate_prompt(
            generate.instruction,
            plan.style_prompt,
            self._cfg.generate.num_per_call,
            plan.seed_texts,
            generate.temperature,
        )
        try:
            result = await ctx.schema_engine.complete_validated(
                plan.llm,
                prompt,
                schema=schema,
                scope=CallScope(record_ids=plan.seed_ids, batch_no=ctx.batch_no),
            )
            return list(result[0]["samples"])
        except CircuitBreakerTripped:
            raise
        except LabelKitError as exc:
            self._handle_voided_call(plan, exc, ctx)
            return None

    def _log_unfittable(self, plan: CallPlan, ctx: "RunContext") -> None:
        """记录预算预检作废调用。

        @param plan 作废调用计划。
        @param ctx 运行上下文。
        @return None。
        """
        exc = ContextOverflowError(
            "generation call unfittable at 1 seed",
            phase="precheck",
            profile=plan.llm,
        )
        _log.warning(
            void_log_message(plan, exc), extra={"stage": self.name, "batch": ctx.batch_no}
        )

    def _handle_voided_call(
        self,
        plan: CallPlan,
        exc: LabelKitError,
        ctx: "RunContext",
    ) -> None:
        """按既有 flat 语义记录可恢复作废调用。

        @param plan 作废调用计划。
        @param exc 捕获到的 LabelKitError。
        @param ctx 运行上下文。
        @return None。
        """
        if (
            isinstance(exc, ContextOverflowError)
            and exc.phase == "reactive"
            and getattr(exc, "origin", "http_400") == "http_400"
            and not getattr(exc, "_breaker_fed", False)
        ):
            exc._breaker_fed = True  # type: ignore[attr-defined]
            ctx.metrics.record_provider_result(fatal=True)
        _log.warning(
            void_log_message(plan, exc), extra={"stage": self.name, "batch": ctx.batch_no}
        )

    @staticmethod
    def _make_item(record: Record, class_name: str | None) -> PipelineItem:
        """装配一个平面生成 PipelineItem。

        @param record 新生成记录。
        @param class_name inherited 类名或 None。
        @return 新流水线项。
        """
        if class_name is None:
            return PipelineItem(record=record)
        return PipelineItem(
            record=record,
            classification=Classification(
                label=class_name,
                labels=(class_name,),
                source="inherited",
                detail={},
            ),
        )
