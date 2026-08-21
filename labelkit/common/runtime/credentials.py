"""v1.17 secret-free 配置的运行期凭据载体（SPEC-SP §5.2 / CONTRACTS §7.19.3）。

profile 只保存环境变量名；密钥值仅在 ``run`` 与 ``validate --probe`` 两条真实
网络路径上经 :func:`resolve_credentials` 物化进 :class:`RuntimeCredentials`——
静态 validate 与 ``run --dry-run`` 全程不调用任何环境变量 value reader。本模块
同时是 ``referenced_profiles`` 收集器的唯一属主（原 orchestration 层的第二份
实现已删除，无 shim），静态校验、凭据物化、探测、运行期与估算共用它。

密钥值没有任何显示面：无 repr、无异常文本、无序列化路径；两个 mapping 在构造
时复制为只读映射（``MappingProxyType`` 天然拒绝 deepcopy / pickle / asdict）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Mapping

from labelkit.common.errors import ConfigError

if TYPE_CHECKING:
    from labelkit.common.config.model import LLMProfile, EmbeddingProfile, ResolvedConfig

__all__ = ["RuntimeCredentials", "referenced_profiles", "resolve_credentials"]


def _freeze_pool(table: Mapping[str, tuple[str, ...]],
                 kind: str) -> Mapping[str, tuple[str, ...]]:
    """把一个 profile → key tuple 表复制成键排序的只读映射。

    @param table 调用方提供的原始表
    @param kind 表的种类名（"llm" / "embedding"，仅用于错误定位）
    @return 键按 profile name 排序的只读映射；value 为去重保序的非空 tuple
    @raises ValueError 存在空 key tuple（消息只含剖面名，绝不含密钥值）
    """
    frozen: dict[str, tuple[str, ...]] = {}
    for name in sorted(table):
        values = tuple(dict.fromkeys(table[name]))
        if not values:
            raise ValueError(
                f"runtime credential pool for {kind} profile {name!r} "
                "must be non-empty")
        frozen[name] = values
    return MappingProxyType(frozen)


@dataclass(frozen=True, repr=False)
class RuntimeCredentials:
    """仅真实网络运行持有的 profile 密钥值（CONTRACTS §7.19.3 冻结块）。"""

    llm: Mapping[str, tuple[str, ...]]
    embedding: Mapping[str, tuple[str, ...]]

    def __post_init__(self) -> None:
        """两个 mapping 复制为键排序的只读映射；value 去重保序且非空。"""
        object.__setattr__(self, "llm", _freeze_pool(self.llm, "llm"))
        object.__setattr__(self, "embedding", _freeze_pool(self.embedding, "embedding"))


def referenced_profiles(cfg: "ResolvedConfig") -> tuple[list[str], list[str]]:
    """返回被启用阶段引用的 LLM 与 embedding profile 名（保序、去重）。

    v1.17（§5.2）：收集器自 ``labelkit/orchestration/profile_usage.py`` 下沉至
    common 层且全仓唯一——静态校验、凭据物化、探测、运行期与估算共用本实现，
    语义与下沉前逐字一致。

    @param cfg: 已解析配置
    @return: (LLM profile 名列表, embedding profile 名列表)
    """
    llm_names: list[str] = []
    if cfg.segment.enabled and cfg.segment.strategy in ("llm", "hybrid"):
        llm_names.append(cfg.segment.llm)
    if cfg.stitch.enabled:
        llm_names.append(cfg.stitch.llm)
    if cfg.classify.enabled:
        llm_names.append(cfg.classify.llm)
    if cfg.frame_classify.enabled:
        # v1.12：帧级分类判决 profile——enabled 即入探测集（链位居序列级 classify 之后；
        # 永不入 vision 必需集，vision 语义分列裁决）
        llm_names.append(cfg.frame_classify.llm)
    if cfg.extract.enabled:
        llm_names.append(cfg.extract.llm)
    if cfg.quality.enabled:
        if cfg.quality.mode == "pointwise" or not cfg.quality.judges:
            llm_names.append(cfg.quality.llm)
        else:
            llm_names.extend(cfg.quality.judges)
    if cfg.annotate.enabled:
        llm_names.append(cfg.annotate.llm)
    if cfg.frame_annotate.enabled:
        # v1.12：帧级标注 profile——enabled 即入探测集（链位居序列级 annotate 之后）
        llm_names.append(cfg.frame_annotate.llm)
    if cfg.generate.enabled:
        llm_names.extend(cfg.generate.llms)
    if cfg.verify.enabled:
        if cfg.verify.judges:
            llm_names.extend(cfg.verify.judges)
        else:
            llm_names.append(cfg.verify.llm)
    if cfg.output.repair_llm:
        llm_names.append(cfg.output.repair_llm)

    emb_names: list[str] = []
    if cfg.dedup.enabled and cfg.dedup.semantic and cfg.dedup.semantic_embedding:
        emb_names.append(cfg.dedup.semantic_embedding)
    return list(dict.fromkeys(llm_names)), list(dict.fromkeys(emb_names))


def _declared_env_names(prof: "LLMProfile | EmbeddingProfile") -> tuple[str, ...]:
    """取剖面声明的密钥环境变量名（声明序、按名去重）。

    @param prof [llm.*] 或 [embedding.*] 剖面
    @return 环境变量名 tuple；两种写法都未声明时为空 tuple
    """
    envs = tuple(prof.api_key_envs) or (
        (prof.api_key_env,) if prof.api_key_env else ())
    return tuple(dict.fromkeys(envs))


def _resolve_profile_keys(prof: "LLMProfile | EmbeddingProfile | None", label: str,
                          errors: list[str]) -> tuple[str, ...]:
    """读单个剖面声明的全部环境变量值；缺失项聚合进 errors。

    @param prof 剖面对象；None = 引用了未声明剖面（M1 已拦，防御性上报）
    @param label 错误定位前缀（如 ``[llm.default]``）
    @param errors 聚合错误输出列表（就地追加；消息只含环境变量名）
    @return 已解析的密钥值 tuple（声明序）；全部缺失时为空 tuple
    """
    if prof is None:
        errors.append(f"{label}: referenced profile is not declared")
        return ()
    values: list[str] = []
    for env in _declared_env_names(prof):
        value = os.environ.get(env)
        if not value:
            errors.append(
                f'{label}.api_key_envs: environment variable "{env}" is not set or '
                "empty (required by run and validate --probe; static validate and "
                "dry-run never read key values)")
            continue
        values.append(value)
    return tuple(values)


def resolve_credentials(cfg: "ResolvedConfig") -> RuntimeCredentials:
    """对所有被引用 profile 聚合解析密钥值（run / validate --probe 的唯一物化点）。

    任一被引用剖面的任一环境变量缺失或为空 ⇒ 聚合上报**全部**缺失项
    （ConfigError，CLI 退出码 2）；未被引用的剖面永不解析（v1.6 rule 12 口径）。
    静态 validate 与 ``run --dry-run`` 绝不调用本函数。

    @param cfg 已解析配置（profile 只携带环境变量名）
    @return 冻结的运行期凭据
    @raises ConfigError 聚合的全部缺失密钥项
    """
    llm_names, emb_names = referenced_profiles(cfg)
    errors: list[str] = []
    llm_table: dict[str, tuple[str, ...]] = {}
    for name in llm_names:
        keys = _resolve_profile_keys(cfg.llm_profiles.get(name), f"[llm.{name}]",
                                     errors)
        if keys:
            llm_table[name] = keys
    emb_table: dict[str, tuple[str, ...]] = {}
    for name in emb_names:
        keys = _resolve_profile_keys(cfg.embedding_profiles.get(name),
                                     f"[embedding.{name}]", errors)
        if keys:
            emb_table[name] = keys
    if errors:
        raise ConfigError(errors)
    return RuntimeCredentials(llm=llm_table, embedding=emb_table)
