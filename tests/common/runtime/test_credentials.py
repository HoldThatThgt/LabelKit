"""v1.17 Wave 2b 凭据面离线单测：RuntimeCredentials 冻结块、referenced_profiles
下沉等价（对照旧 profile_usage 行为的黄金用例）、resolve_credentials 聚合/去重/
保序，以及「静态面零环境变量 value 读」的命令分流断言。全程零网络。"""
from __future__ import annotations

import copy
import os
import pickle
from pathlib import Path

import pytest

from labelkit.common.config.model import CliOverrides
from labelkit.common.errors import EXIT_OK, ConfigError
from labelkit.common.runtime import credentials as creds_mod
from labelkit.common.runtime.credentials import (
    RuntimeCredentials,
    referenced_profiles,
    resolve_credentials,
)
from labelkit.orchestration.runtime import execute_run, probe_referenced_profiles
from tests.common.config.test_config import BASE_CONFIG, Env, env  # noqa: F401 (fixture)
from tests.cli.test_cli import _cfg  # 下沉等价黄金用例复用同一配置工厂

# ── referenced_profiles 下沉等价（黄金用例对照旧 profile_usage 行为） ───────


def test_referenced_profiles_default_matches_golden():
    """黄金用例（对照 tests/cli/test_cli.py::test_referenced_profiles_default）。"""
    llms, embs = referenced_profiles(_cfg())
    assert llms == ["default"]
    assert embs == []


def test_referenced_profiles_all_stages_matches_golden():
    """黄金用例：全阶段启用时的保序去重 + embedding 腿。"""
    from labelkit.common.config.model import (
        DedupConfig,
        GenerateConfig,
        OutputConfig,
        QualityConfig,
        VerifyConfig,
    )

    cfg = _cfg(
        quality=QualityConfig(judges=("default", "judge", "fixer")),
        generate=GenerateConfig(enabled=True, instruction="生成",
                                llms=("default", "judge")),
        verify=VerifyConfig(enabled=True, llm="judge"),
        output=OutputConfig(schema_inline="{}", repair_llm="fixer"),
        dedup=DedupConfig(semantic=True, semantic_embedding="emb"),
    )
    llms, embs = referenced_profiles(cfg)
    assert llms == ["default", "judge", "fixer"]
    assert embs == ["emb"]


def test_referenced_profiles_judges_replace_goldens():
    """黄金用例：pairwise judges 替换 quality.llm；pointwise 无视 judges；
    verify judges 替换 verify.llm；禁用阶段不入集。"""
    from labelkit.common.config.model import (
        AnnotateConfig,
        QualityConfig,
        VerifyConfig,
    )

    llms, _ = referenced_profiles(_cfg(
        quality=QualityConfig(mode="pairwise", llm="fixer", judges=("judge",)),
        annotate=AnnotateConfig(enabled=False, instruction="标注")))
    assert llms == ["judge"]

    llms, _ = referenced_profiles(_cfg(
        quality=QualityConfig(mode="pointwise", llm="default",
                              judges=("judge", "fixer", "judge")),
        annotate=AnnotateConfig(enabled=False, instruction="标注")))
    assert llms == ["default"]

    llms, _ = referenced_profiles(_cfg(
        quality=QualityConfig(enabled=False),
        annotate=AnnotateConfig(enabled=True, llm="default", instruction="标注"),
        verify=VerifyConfig(enabled=True, llm="fixer", judges=("judge",))))
    assert llms == ["default", "judge"]


def test_referenced_profiles_source_is_the_common_layer_only():
    """CONTRACTS §7.19.3：收集器全仓唯一——旧 orchestration 模块已删除（无 shim），
    runtime 与 orchestrator 消费的都下沉后的同一函数对象。"""
    import labelkit.orchestration
    import labelkit.orchestration.orchestrator as orchestrator_mod
    import labelkit.orchestration.runtime as runtime_mod

    assert runtime_mod.referenced_profiles is referenced_profiles
    assert orchestrator_mod.referenced_profiles is referenced_profiles
    assert not hasattr(labelkit.orchestration, "referenced_profiles")
    with pytest.raises(ModuleNotFoundError):
        import labelkit.orchestration.profile_usage  # noqa: F401


# ── resolve_credentials：聚合 / 去重 / 保序 / 未引用豁免 ────────────────────

POOL_CONFIG = BASE_CONFIG.replace(
    'api_key_env = "LK_TEST_KEY_DEFAULT"',
    'api_key_envs = ["LK_TEST_KEY_A", "LK_TEST_KEY_B"]',
    1,
)


def test_resolve_reads_referenced_profiles_in_declaration_order(env):
    """声明序读值；mapping 键按 profile name 排序。"""
    body = '[dedup]\nsemantic = true\nsemantic_embedding = "emb"'
    cfg = env.load(project_text=env.project(body=body))
    creds = resolve_credentials(cfg)
    assert creds.llm["default"] == ("sk-default",)
    assert creds.embedding["emb"] == ("sk-emb",)
    assert list(creds.llm) == ["default"]


def test_resolve_pool_values_keep_declaration_order(env, monkeypatch):
    """池化剖面：value tuple 保持环境变量声明序（未去重时 1:1 对齐 env 名）。"""
    monkeypatch.setenv("LK_TEST_KEY_A", "sk-a")
    monkeypatch.setenv("LK_TEST_KEY_B", "sk-b")
    cfg = env.load(config_text=POOL_CONFIG)
    creds = resolve_credentials(cfg)
    assert creds.llm["default"] == ("sk-a", "sk-b")


def test_resolve_aggregates_every_missing_key_across_kinds(env, monkeypatch):
    """任一缺失 ⇒ 聚合上报**全部**缺失项（llm 池 + embedding 两腿），ConfigError。"""
    for name in ("LK_TEST_KEY_A", "LK_TEST_KEY_B", "LK_TEST_KEY_EMB"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LK_TEST_KEY_DEFAULT", "sk-default")
    body = '[dedup]\nsemantic = true\nsemantic_embedding = "emb"'
    cfg = env.load(config_text=POOL_CONFIG, project_text=env.project(body=body))
    with pytest.raises(ConfigError) as ei:
        resolve_credentials(cfg)
    lines = ei.value.errors
    assert len(lines) == 3
    assert any('"LK_TEST_KEY_A"' in line and "[llm.default]" in line
               for line in lines)
    assert any('"LK_TEST_KEY_B"' in line and "[llm.default]" in line
               for line in lines)
    assert any('"LK_TEST_KEY_EMB"' in line and "[embedding.emb]" in line
               for line in lines)
    assert all("is not set or empty" in line for line in lines)


def test_resolve_empty_value_counts_as_missing(env, monkeypatch):
    monkeypatch.setenv("LK_TEST_KEY_DEFAULT", "")   # 空串 = 缺失
    cfg = env.load()
    with pytest.raises(ConfigError) as ei:
        resolve_credentials(cfg)
    assert any('"LK_TEST_KEY_DEFAULT"' in line for line in ei.value.errors)


def test_resolve_skips_unreferenced_profiles(env, monkeypatch):
    """rule 12 口径：未被引用的剖面（judge）永不解析——缺 key 不报错。"""
    monkeypatch.delenv("LK_TEST_KEY_JUDGE", raising=False)
    cfg = env.load()
    creds = resolve_credentials(cfg)
    assert "judge" not in creds.llm


def test_probe_command_face_fails_closed_before_any_network(env, monkeypatch):
    """validate --probe 的探测入口：缺 key 先聚合 ConfigError（exit 2 面），
    绝不构造客户端去撞端点——离线可完整承保的路由断言。"""
    monkeypatch.delenv("LK_TEST_KEY_DEFAULT", raising=False)
    cfg = env.load()
    with pytest.raises(ConfigError):
        probe_referenced_profiles(cfg)


# ── RuntimeCredentials 冻结块（CONTRACTS §7.19.3 逐字） ─────────────────────


def test_credentials_freezes_sorted_readonly_mappings():
    creds = RuntimeCredentials(llm={"b": ("k2",), "a": ("k1",)}, embedding={})
    assert list(creds.llm) == ["a", "b"]           # 键按 profile name 排序
    with pytest.raises(TypeError):
        creds.llm["c"] = ("k3",)                   # 只读映射拒绝写
    with pytest.raises(AttributeError):
        creds.llm = {}                             # frozen 实例拒绝改字段


def test_credentials_dedups_values_preserving_declaration_order():
    """值去重、保持首个声明序（§7.19.3「去重后、保持环境变量声明顺序」）。"""
    creds = RuntimeCredentials(llm={"p": ("k1", "k1", "k2", "k1")},
                               embedding={})
    assert creds.llm["p"] == ("k1", "k2")


def test_credentials_rejects_empty_pools_without_leaking_values():
    with pytest.raises(ValueError, match="non-empty") as ei:
        RuntimeCredentials(llm={"p": ()}, embedding={})
    assert "k" not in str(ei.value)                # 错误消息不含密钥值


def test_credentials_have_no_secret_display_or_serialization_surface():
    secret = "sk-NEVER-SHOW-ME"
    creds = RuntimeCredentials(llm={"p": (secret,)}, embedding={})
    assert secret not in repr(creds)               # repr=False 的 dataclass 面
    assert secret not in str(creds)
    for blocked in (copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError):
            blocked(creds)                         # MappingProxyType 天然拒绝
    import dataclasses

    with pytest.raises(TypeError):
        dataclasses.asdict(creds)                  # asdict 内部 deepcopy 同样被拒


def test_credentials_equality_never_compares_secret_values():
    assert (RuntimeCredentials(llm={"p": ("first-secret",)}, embedding={})
            == RuntimeCredentials(llm={"p": ("different-secret",)}, embedding={}))


def test_sequence_referenced_profiles_follow_the_fixed_stage_order():
    root = Path(__file__).resolve().parents[3] / "examples" / "sequence-generation"
    from labelkit.common.config import load

    cfg = load(root / "config.toml", root / "project.toml", CliOverrides())
    llms, embeddings = referenced_profiles(cfg)
    assert llms == ["default", "judge"]
    assert embeddings == []


# ── 命令分流：静态面零环境变量 value 读（SPEC-SP §5.2） ─────────────────────


def test_static_validate_and_dry_run_never_read_env_values(env, monkeypatch, capsys):
    """validate（无 --probe）与 run --dry-run 在**密钥环境变量**的读取被毒化的
    情况下完整走通——orchestration/cli 侧不存在第二条 credential value 读路径。
    毒化按名定向：第三方库（如 numpy 首次导入时读自家的调优变量）的非秘密 env
    读不属于本纪律（SPEC-SP §5.2 的对象是密钥 value reader）。"""
    import labelkit.orchestration.runtime as runtime_mod

    env.load()  # 静态装载 keyless（Wave 2a），先写出 config/project 文件
    key_envs = {"LK_TEST_KEY_DEFAULT", "LK_TEST_KEY_JUDGE", "LK_TEST_KEY_EMB",
                "LK_TEST_KEY_A", "LK_TEST_KEY_B"}
    real_get = os.environ.get

    def _boom(name, default=None):  # noqa: ANN001
        if name in key_envs:
            raise AssertionError(
                f"static face must not read credential values (got {name!r})")
        return real_get(name, default)

    monkeypatch.setattr(os.environ, "get", _boom)
    cfg = runtime_mod.validate_project(env.tmp / "config.toml",
                                       env.tmp / "project.toml")
    assert cfg.paths.output.endswith(".jsonl")
    assert execute_run(env.tmp / "config.toml", env.tmp / "project.toml",
                       CliOverrides(dry_run=True)) == EXIT_OK
    capsys.readouterr()                            # 静音 dry-run 输出
    assert creds_mod.os.environ.get is _boom       # 毒化贯穿全程未被换回
