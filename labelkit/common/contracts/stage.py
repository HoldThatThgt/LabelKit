"""Stage protocol (spec §4.3) and RunContext (spec §3.10.3). Frozen contract."""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from labelkit.common.config.model import ResolvedConfig
    from labelkit.common.runtime.llm_client import LLMClient
    from labelkit.common.runtime.schema_engine import SchemaEngine
    from labelkit.common.observability.obslog import MetricsSink
    from labelkit.common.contracts.types import PipelineItem


@dataclass
class RunContext:
    """Context handed to every stage.run() invocation. Constructed by M10 orchestrator,
    ONE PER (batch, stage) INVOCATION, because rng is derived per batch and stage.
    Exactly the six fields of spec 3.10.3 — spec 3.12.3 explicitly forbids extending this
    signature; run_id/run_started_at travel via the MetricsSink/Emitter/Orchestrator
    constructors instead (§7.9–§7.11)."""
    cfg: ResolvedConfig
    llm: LLMClient
    schema_engine: SchemaEngine
    metrics: MetricsSink
    rng: random.Random            # random.Random(f"{cfg.run.seed}:{batch_no}:{stage_name}")
    batch_no: int                 # 1-based; run-level events use 0


class Stage(Protocol):
    name: str

    async def run(self, batch: list[PipelineItem], ctx: RunContext) -> list[PipelineItem]:
        """契约：① 只处理 status=='active' 的项；② 不删除列表元素（只改 status）；
           ②a classify 例外（仅 assignment="multi"）——可向传入列表尾部追加派生信封；
           追加物视同批内普通元素、同受 ①③④ 约束；不得删除、重排或替换任何既有元素对象
           （既有元素的 status / classification / errors 字段写入属 ①④ 的正常行为）；
           返回值仍须是传入的同一列表对象（调用方依赖列表身份）；
           ②b segment 例外（v1.8，仅 stream 模式）——segment 可将批内既有 active 成员信封的
           status 置为 absorbed 或 dropped_noise（属①④的正常状态写入），并向传入列表
           尾部追加以这些成员拼装的序列信封；追加物视同批内普通元素、同受①③④约束；
           每个成员信封至多被一个序列信封吸收；不得删除、重排或替换任何既有元素对象；
           返回值仍须是传入的同一列表对象。M7 修复路径豁免：verify 的缺陷修复可在本批内
           将成员信封状态在 absorbed 与 dropped_noise 间双向改写（成员回收/收缩），
           此为契约①的唯一反向豁免；禁止将成员信封翻回 active；
           ③ generate 例外——返回新增子批（原批元素不修改）；④ 单条失败不得抛出到批层面，
           必须落入 item.errors 并置 status='failed'。"""
        ...
