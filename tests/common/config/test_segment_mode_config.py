"""segment.mode 配置面的离线测试：键的模式合法性、rule 72–80、D9 / D10 与配置侧重键。

纯逻辑，不涉 LLM、不涉网络。错误文本按 CONTRACTS §6.3 冻结，这里按整行比较。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from labelkit.common.config import ResolvedConfig, load
from labelkit.common.config._collect import _Collector
from labelkit.common.config._postprocessing import check_region_refs
from labelkit.common.config._schemas import _GenerationSchemaRequest, _load_generation_schema
from labelkit.common.config.model import CliOverrides, SegmentConfig
from labelkit.common.errors import ConfigError
from labelkit.common.inference import budget
from labelkit.common.inference.credentials import referenced_profiles

SCHEMA = json.dumps({"type": "object", "properties": {"topic": {"type": "string"}},
                     "required": ["topic"], "additionalProperties": False})

PROFILES = {
    "default": "supports_vision = true\ncontext_window = 131072\n",
    "vision": "supports_vision = true\ncontext_window = 131072\n",
    "plain": "supports_vision = false\ncontext_window = 131072\n",
    "nowindow": "supports_vision = true\n",
    "tiny": "supports_vision = true\ncontext_window = 8192\n",
}
IMAGE = 'enabled = true\nmode = "image"\ninstruction = "生成候选区域"\n'
BOXES = 'enabled = true\nmode = "image"\nbox_parser = "hooks.py:parse"\ncompanion_suffix = ".xml"\n'
SEQUENCE = 'enabled = true\nmode = "sequence"\n'
QUALITY_OFF = "enabled = false\n"
PROJECT_DEFAULTS = {"quality": QUALITY_OFF, "annotate": 'instruction = "标注"\n', "output": "", "modality": "ui",
                    "run": "", "body": ""}
HOOKS = '''\
def parse(companion, image):
    raise AssertionError("M1 must never call box_parser")


async def async_parse(companion, image):
    return []
'''


def config_text(names=tuple(PROFILES), embedding_window=None):
    """组装 config.toml；每个 profile 的能力由名字决定。"""
    parts = ["schema_version = 1\n"]
    for name in names:
        parts.append(f'[llm.{name}]\nprovider = "openai_compatible"\nbase_url = "https://example.com/v1"\n'
                     f'model = "{name}-model"\napi_key_env = "LK_TEST_KEY"\n'
                     f"supports_structured_output = true\n{PROFILES[name]}\n")
    if embedding_window is not None:
        parts.append('[embedding.emb]\nbase_url = "https://example.com/v1"\nmodel = "bge"\n'
                     f'api_key_env = "LK_TEST_KEY"\ncontext_window = {embedding_window}\n')
    return "".join(parts)


class Env:
    """在临时目录里写两份 TOML 与钩子文件，再走 M1 唯一入口。"""

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        (tmp_path / "capture").mkdir()
        (tmp_path / "out").mkdir()
        (tmp_path / "hooks.py").write_text(HOOKS, encoding="utf-8")
        self.project_path = tmp_path / "project.toml"
        self.config_path = tmp_path / "config.toml"
        self.fp = str(self.project_path)
        self.fc = str(self.config_path)

    def project(self, segment=IMAGE, **sections) -> str:
        """组装 project.toml；``None`` 表示整节不写。

        @param segment ``[segment]`` 节正文
        @param sections 覆盖 ``PROJECT_DEFAULTS`` 的键：quality、annotate、output、modality、run、body
        @return project.toml 文本
        """
        unknown = sections.keys() - PROJECT_DEFAULTS.keys()
        assert not unknown, f"unknown project section: {sorted(unknown)}"
        values = {**PROJECT_DEFAULTS, **sections}
        quality, annotate, output, modality, run, body = (values[key] for key in PROJECT_DEFAULTS)
        capture = (self.tmp / "capture").as_posix()
        target = (self.tmp / "out" / "result.jsonl").as_posix()
        parts = [f'schema_version = 1\n\n[run]\ninput = "{capture}"\noutput = "{target}"\n'
                 f'modality = "{modality}"\n{run}\n']
        if segment is not None:
            parts.append(f"[segment]\n{segment}\n")
        if quality is not None:
            parts.append(f"[quality]\n{quality}\n")
        parts.append(f"[annotate]\n{annotate}\n")
        if body:
            parts.append(body + "\n")
        parts.append(f"[output]\n{output}schema_inline = '''{SCHEMA}'''\n")
        return "".join(parts)

    def load(self, project_text: str | None = None, config: str | None = None) -> ResolvedConfig:
        self.config_path.write_text(config_text() if config is None else config, encoding="utf-8")
        self.project_path.write_text(self.project() if project_text is None else project_text, encoding="utf-8")
        return load(self.config_path, self.project_path, CliOverrides())

    def errors(self, project_text: str | None = None, config: str | None = None) -> list[str]:
        with pytest.raises(ConfigError) as caught:
            self.load(project_text, config)
        return caught.value.errors


@pytest.fixture
def env(tmp_path) -> Env:
    return Env(tmp_path)


# ── mode ───────────────────────────────────────────────────────────────────


def test_rule72_mode_required_when_enabled(env):
    expected = (f'{env.fp}:[segment].mode: required when segment.enabled = true; '
                'expected "image" or "sequence"')
    assert env.errors(env.project("enabled = true\n")) == [expected]
    # 缺 mode 时只报 rule 72，流家族前置条件不混入旧文本，也不报 rule 76。
    body = "[stitch]\nenabled = true\n\n[extract]\nenabled = true\n"
    assert env.errors(env.project("enabled = true\n", body=body)) == [expected]


def test_rule72_alone_when_mode_missing_with_annotate_off(env):
    """缺 mode 时还不是流模式：rule 29 的 annotate 项只属于 sequence，不提前要求打开 annotate。"""
    project = env.project("enabled = true\n", annotate="enabled = false\n", quality='rubric = "default:ui"\n')
    assert env.errors(project) == [f'{env.fp}:[segment].mode: required when segment.enabled = true; '
                                   'expected "image" or "sequence"']


def test_unknown_mode_with_annotate_off_reports_only_the_enum(env):
    project = env.project('enabled = true\nmode = "video"\n', annotate="enabled = false\n",
                          quality='rubric = "default:ui"\n')
    assert env.errors(project) == [f'{env.fp}:[segment].mode: expected "image" | "sequence", got "video"']


@pytest.mark.parametrize("value", ["images", "stream", "IMAGE", ""])
def test_mode_rejects_unknown_value(env, value):
    expected = f'{env.fp}:[segment].mode: expected "image" | "sequence", got {json.dumps(value)}'
    # 枚举在解析层恒检查：开关关闭时同样报错；写了非法值时不再追加「缺键」。
    assert env.errors(env.project(f'enabled = true\nmode = "{value}"\n')) == [expected]
    assert env.errors(env.project(f'enabled = false\nmode = "{value}"\n')) == [expected]


def test_sequence_resolved_is_a_frozen_parse_product(env, capsys):
    assert env.load(env.project(SEQUENCE)).segment.sequence_resolved is True
    assert env.load(env.project(IMAGE)).segment.sequence_resolved is False
    parked = env.load(env.project('enabled = false\nmode = "sequence"\n'))
    assert parked.segment.sequence_resolved is False
    capsys.readouterr()
    # 解析产物永非用户键：写了也只是未知键告警，不改变解析结果。
    written = env.load(env.project(IMAGE + "sequence_resolved = true\n"))
    assert written.segment.sequence_resolved is False
    assert "[segment].sequence_resolved: unknown key, ignored" in capsys.readouterr().err


def test_disabled_keys_are_parked_with_warning(env, capsys):
    parked = ('enabled = false\nmode = "image"\ninstruction = "   "\nbox_parser = "ghost.py:fn"\n'
              'companion_suffix = ".png"\nstrategy = "rules"\n')
    cfg = env.load(env.project(parked, quality=None))
    err = capsys.readouterr().err
    assert f"{env.fp}:[segment].enabled: segment.enabled = false" in err
    assert cfg.segment.resolved_box_parser is None
    assert cfg.segment.llm_referenced is False
    assert cfg.segment.vision_resolved is False
    assert cfg.quality.rubric == "default:ui"


def test_llm_referenced_is_false_without_enabled_or_mode():
    assert SegmentConfig(mode="image", instruction="x").llm_referenced is False
    assert SegmentConfig(enabled=True).llm_referenced is False
    assert SegmentConfig(enabled=True, mode="image").llm_referenced is False
    assert SegmentConfig(enabled=True, mode="image", instruction="  ").llm_referenced is False
    assert SegmentConfig(enabled=True, mode="image", instruction="x").llm_referenced is True
    assert SegmentConfig(enabled=True, mode="sequence", strategy="rules").llm_referenced is False
    assert SegmentConfig(enabled=True, mode="sequence", strategy="llm").llm_referenced is True


# ── llm ────────────────────────────────────────────────────────────────────


def test_image_llm_referenced_with_instruction(env):
    cfg = env.load(env.project(IMAGE + 'llm = "vision"\n'))
    assert cfg.segment.llm == "vision"
    assert cfg.segment.llm_referenced is True
    assert referenced_profiles(cfg)[0] == ["vision", "default"]
    errors = env.errors(env.project(IMAGE + 'llm = "ghost"\n'))
    assert len(errors) == 1
    assert errors[0].startswith(f'{env.fp}:[segment].llm: referenced profile "ghost" does not exist')


def test_image_explicit_llm_without_instruction_rejected(env):
    expected = (f'{env.fp}:[segment].llm: not allowed when segment.mode = "image" has no instruction '
                "(no segment model call is made)")
    assert env.errors(env.project(BOXES + 'llm = "vision"\n')) == [expected]


def test_boxes_only_segment_llm_not_referenced(env):
    # default 既无视觉也无窗口：segment.llm 的默认值若入集就会触发 rule 77a、77b。
    weak_default = ('[llm.default]\nprovider = "openai_compatible"\nbase_url = "https://example.com/v1"\n'
                    'model = "weak"\napi_key_env = "LK_TEST_KEY"\nsupports_vision = false\n')
    project = env.project(BOXES, annotate='instruction = "标注"\nllm = "vision"\n',
                          quality='enabled = false\nllm = "vision"\n')
    cfg = env.load(project, config_text(("vision",)) + weak_default)
    assert cfg.segment.llm == "default"
    assert cfg.segment.llm_referenced is False
    assert cfg.segment.vision_resolved is False
    assert referenced_profiles(cfg) == (["vision"], [])


@pytest.mark.parametrize("strategy,referenced", [("rules", False), ("llm", True), ("hybrid", True)])
def test_sequence_llm_reference_follows_strategy(env, strategy, referenced):
    segment = SEQUENCE + f'strategy = "{strategy}"\nllm = "vision"\nnoise_filter = false\n'
    cfg = env.load(env.project(segment))
    assert ("vision" in referenced_profiles(cfg)[0]) is referenced
    assert cfg.segment.vision_resolved is referenced
    if not referenced:
        # rules 策略不拨号：不存在的 profile 也不报错。
        env.load(env.project(segment.replace('"vision"', '"ghost"')))


def test_image_mode_segment_llm_in_reference_and_vision_sets(env):
    cfg = env.load(env.project(IMAGE + 'llm = "vision"\n'))
    assert "vision" in referenced_profiles(cfg)[0]
    assert cfg.segment.vision_resolved is True
    # 引用集：不存在即报错；视觉集：不支持视觉即报错；窗口集：未声明即报错。
    assert "[segment].llm: referenced profile" in env.errors(env.project(IMAGE + 'llm = "ghost"\n'))[0]
    assert "[llm.plain].supports_vision" in env.errors(env.project(IMAGE + 'llm = "plain"\n'))[0]
    assert "[llm.nowindow].context_window" in env.errors(env.project(IMAGE + 'llm = "nowindow"\n'))[0]


# ── instruction / box_parser / companion_suffix ────────────────────────────


@pytest.mark.parametrize("value", ["", "   ", "\\n\\t"])
def test_image_instruction_blank_rejected(env, value):
    expected = f"{env.fp}:[segment].instruction: must be non-empty when set"
    # 写了空白 instruction 只报 73d，不连带 rule 74 或 73c。
    assert env.errors(env.project(f'enabled = true\nmode = "image"\ninstruction = "{value}"\n'
                                  'llm = "vision"\n')) == [expected]


@pytest.mark.parametrize("key,value", [("instruction", '"指令"'), ("box_parser", '"hooks.py:parse"'),
                                       ("companion_suffix", '".xml"')])
def test_sequence_rejects_image_keys(env, key, value):
    expected = (f'{env.fp}:[segment].{key}: not allowed when segment.mode = "sequence" '
                "(image segmentation key)")
    assert env.errors(env.project(SEQUENCE + f"{key} = {value}\n")) == [expected]


def test_box_parser_loaded_and_frozen(env):
    # HOOKS.parse 一旦被调用就抛 AssertionError：能走完 load 就证明 M1 没有调用它。
    cfg = env.load(env.project(BOXES))
    hook = cfg.segment.resolved_box_parser
    assert hook.target.__name__ == "parse"
    assert hook.reference == f"{Path(env.fp).resolve().parent / 'hooks.py'}:parse"
    assert cfg.segment.box_parser == "hooks.py:parse"
    assert cfg.segment.companion_suffix == ".xml"
    assert hook is not None and cfg.validation_hooks.output is None
    assert not hasattr(cfg.validation_hooks, "box_parser")
    # instruction 与 box_parser 可以同时存在；只写 instruction 时没有钩子。
    assert env.load(env.project(IMAGE + BOXES.split("\n", 2)[2])).segment.resolved_box_parser is not None
    assert env.load(env.project(IMAGE)).segment.resolved_box_parser is None


@pytest.mark.parametrize("reference", ["ghost.py:fn", "hooks.py:ghost", "hooks.txt:parse", "module.fn"])
def test_box_parser_load_errors(env, reference):
    segment = f'enabled = true\nmode = "image"\nbox_parser = "{reference}"\ncompanion_suffix = ".xml"\n'
    assert env.errors(env.project(segment)) == [
        f"{env.fp}:[segment].box_parser: cannot load box_parser {reference!r}"]


def test_box_parser_load_and_signature_errors(env):
    bad = BOXES.replace("hooks.py:parse", "hooks.py:async_parse")
    assert env.errors(env.project(bad)) == [
        f"{env.fp}:[segment].box_parser: box_parser must be synchronous with exactly two "
        "positional parameters and no defaults"]
    missing = BOXES.replace("hooks.py:parse", "ghost.py:parse")
    assert env.errors(env.project(missing)) == [
        f"{env.fp}:[segment].box_parser: cannot load box_parser 'ghost.py:parse'"]


def test_companion_suffix_requires_box_parser(env):
    expected = (f"{env.fp}:[segment].companion_suffix: box_parser and companion_suffix must be "
                "set together")
    assert env.errors(env.project(IMAGE + 'box_parser = "hooks.py:parse"\n')) == [expected]
    assert env.errors(env.project(IMAGE + 'companion_suffix = ".xml"\n')) == [expected]


@pytest.mark.parametrize("value", ["xml", "", ".png", ".PNG", ".jpg", ".JPG", ".jpeg", ".JpEg",
                                   ".a/b", ".a\\b", "./x"])
def test_companion_suffix_shape_rules(env, value):
    segment = (f"enabled = true\nmode = \"image\"\nbox_parser = \"hooks.py:parse\"\n"
               f"companion_suffix = '{value}'\n")
    assert env.errors(env.project(segment)) == [
        f'{env.fp}:[segment].companion_suffix: expected a suffix starting with "." that is not an '
        f"image extension, got {json.dumps(value)}"]


@pytest.mark.parametrize("value", [".xml", ".ocr.json", ".JSONL", ".png.xml"])
def test_companion_suffix_legal_shapes(env, value):
    segment = BOXES.replace(".xml", value)
    assert env.load(env.project(segment)).segment.companion_suffix == value


# ── 六个流模式键 ────────────────────────────────────────────────────────────

SEQUENCE_KEYS = [("strategy", '"rules"'), ("window", "1"), ("noise_filter", "false"),
                 ("min_len", "3"), ("context", '"领域"'), ("on_error", '"fail"')]


@pytest.mark.parametrize("key,value", SEQUENCE_KEYS)
@pytest.mark.parametrize("base", [IMAGE, BOXES], ids=["instruction", "boxes"])
def test_image_rejects_sequence_keys(env, base, key, value):
    expected = (f'{env.fp}:[segment].{key}: not allowed when segment.mode = "image" '
                "(sequence segmentation key)")
    # window = 1 只报定向文本，不追加 ">= 2" 的旧文本。
    assert env.errors(env.project(base + f"{key} = {value}\n")) == [expected]


def test_sequence_keys_keep_semantics(env):
    segment = (SEQUENCE + 'strategy = "llm"\nllm = "vision"\nwindow = 8\nnoise_filter = false\n'
               'min_len = 3\ncontext = "领域"\non_error = "fail"\n')
    cfg = env.load(env.project(segment))
    seg = cfg.segment
    assert (seg.strategy, seg.llm, seg.window, seg.noise_filter, seg.min_len, seg.context, seg.on_error) == (
        "llm", "vision", 8, False, 3, "领域", "fail")
    assert seg.sequence_resolved is True and seg.vision_resolved is True
    defaults = env.load(env.project(SEQUENCE)).segment
    assert (defaults.strategy, defaults.window, defaults.on_error) == ("hybrid", 20, "keep")
    errors = env.errors(env.project(SEQUENCE + "window = 1\n"))
    assert len(errors) == 1 and "[segment].window: expected an integer >= 2" in errors[0]


# ── rule 74–80 ─────────────────────────────────────────────────────────────


def test_rule74_requires_instruction_or_box_parser(env):
    assert env.errors(env.project('enabled = true\nmode = "image"\n')) == [
        f'{env.fp}:[segment]: segment.mode = "image" requires at least one of instruction and box_parser']


def test_rule75_requires_ui_modality(env):
    assert env.errors(env.project(IMAGE, modality="text")) == [
        f'{env.fp}:[segment].mode: segment.mode = "image" requires run.modality = "ui", got "text"']


FRAME_CLASSIFY = ('[frame.classify]\nenabled = true\nfallback_class = "a"\n'
                  '[[frame.classify.classes]]\nname = "a"\ndescription = "甲"\n')
FRAME_ANNOTATE = f"[frame.annotate]\nenabled = true\ninstruction = \"帧\"\nschema_inline = '''{SCHEMA}'''\n"
SEQUENCE_STAGES = [
    ("[stitch]\nenabled = true\n",
     '[stitch].enabled: stitch.enabled = true is not available when segment.mode = "image"'),
    ("[extract]\nenabled = true\n",
     '[extract].enabled: extract.enabled = true is not available when segment.mode = "image"'),
    (FRAME_CLASSIFY,
     '[frame.classify].enabled: frame.classify.enabled = true is not available when segment.mode = "image"'),
    (FRAME_ANNOTATE,
     '[frame.annotate].enabled: frame.annotate.enabled = true is not available when segment.mode = "image"'),
    ("[stream]\ngap_steps = 5\n",
     '[stream]: the [stream] section is not available when segment.mode = "image"'),
]


@pytest.mark.parametrize("body,tail", SEQUENCE_STAGES,
                         ids=["stitch", "extract", "frame.classify", "frame.annotate", "stream"])
def test_rule76_sequence_stages_rejected(env, body, tail):
    assert env.errors(env.project(body=body)) == [f"{env.fp}:{tail}"]


def test_rule76_reports_each_error_once(env):
    errors = env.errors(env.project(body="\n".join(body for body, _ in SEQUENCE_STAGES)))
    for _, tail in SEQUENCE_STAGES:
        assert errors.count(f"{env.fp}:{tail}") == 1
    assert len(errors) == len(SEQUENCE_STAGES)
    assert not [error for error in errors if "requires segment.enabled" in error]


def test_preconditions_keep_old_text_when_switch_is_off(env):
    errors = env.errors(env.project('enabled = false\nmode = "sequence"\n',
                                    body="[stitch]\nenabled = true\n\n[extract]\nenabled = true\n"))
    assert any("[stitch].enabled: stitch.enabled = true requires segment.enabled = true" in e for e in errors)
    assert any("[extract].enabled: extract.enabled = true requires segment.enabled = true" in e for e in errors)
    assert not [error for error in errors if "is not available" in error]


CLASSIFY_BODY = ('[classify]\nenabled = true\nllm = "nowindow"\nfallback_class = "a"\n'
                 '[[classify.classes]]\nname = "a"\ndescription = "甲"\n'
                 '[[classify.classes]]\nname = "b"\ndescription = "乙"\n')


@pytest.mark.parametrize("kwargs", [
    {"segment": IMAGE + 'llm = "nowindow"\n'},
    {"annotate": 'instruction = "标注"\nllm = "nowindow"\n'},
    {"quality": 'rubric = "default:ui"\nllm = "nowindow"\n'},
    {"body": CLASSIFY_BODY},
    {"body": '[verify]\nenabled = true\nllm = "nowindow"\n'},
    {"output": 'repair_llm = "nowindow"\n'},
], ids=["segment", "annotate", "quality", "classify", "verify", "repair"])
def test_rule77_context_window_required_for_all_referenced(env, kwargs):
    assert env.errors(env.project(**kwargs)) == [
        f'{env.fc}:[llm.nowindow].context_window: segment.mode = "image" requires a positive '
        "deployment context window for every referenced model profile"]


def test_rule77_unreferenced_profile_may_omit_context_window(env):
    env.load(env.project(BOXES, annotate='instruction = "标注"\nllm = "vision"\n',
                         quality='enabled = false\nllm = "vision"\n'))
    # 窗口要求按图片模式发文本；流模式的旧文本不变。
    errors = env.errors(env.project(SEQUENCE, annotate='instruction = "标注"\nllm = "nowindow"\n'))
    assert errors == [f"{env.fc}:[llm.nowindow].context_window: process sequences require a positive "
                      "deployment context window for every enabled model profile"]


def test_rule77_segment_llm_requires_vision(env):
    assert env.errors(env.project(IMAGE + 'llm = "plain"\n')) == [
        f"{env.fc}:[llm.plain].supports_vision: a profile referenced by the segment stage(s) in UI "
        "modality must have supports_vision = true, got false"]


@pytest.mark.parametrize("annotate", ['instruction = "标注"\n', "enabled = false\n"], ids=["annotate", "segment-only"])
def test_rule77_repair_llm_requires_vision(env, annotate):
    project = env.project(annotate=annotate, output='repair_llm = "plain"\n')
    assert env.errors(project) == [
        f"{env.fc}:[llm.plain].supports_vision: a profile referenced by the output.repair stage(s) in "
        "UI modality must have supports_vision = true, got false"]
    # 不修复就不重放图片。
    env.load(env.project(annotate=annotate, output='repair_llm = "vision"\n'))


def test_rule77_repair_llm_without_repair_rounds_needs_no_vision(env):
    project = env.project(annotate="enabled = false\n",
                          output='repair_llm = "plain"\nmax_repair_attempts = 0\n')
    cfg = env.load(project)
    assert "plain" not in referenced_profiles(cfg)[0]


def test_rule78_semantic_dedup_rejected(env):
    expected = (f'{env.fp}:[dedup].semantic: not available when segment.mode = "image" '
                "(image samples have no text to embed)")
    # 缺 embedding、embedding 窗口未声明都不追加第二条错误。
    assert env.errors(env.project(body="[dedup]\nsemantic = true\n")) == [expected]
    body = '[dedup]\nsemantic = true\nsemantic_embedding = "emb"\n'
    assert env.errors(env.project(body=body), config_text(embedding_window=0)) == [expected]


def test_rule78_ui_tree_max_chars_rejected(env):
    body = "[input]\nui_tree_max_chars = 30000\n"
    assert env.errors(env.project(body=body)) == [
        f'{env.fp}:[input].ui_tree_max_chars: not allowed when segment.mode = "image" '
        "(image mode builds no UI tree)"]
    # 只看显式书写：其余 [input] 键合法，流模式仍可写这个键。
    env.load(env.project(body='[input]\non_missing_pair = "fail"\n'))
    assert env.load(env.project(SEQUENCE, body="[input]\nui_tree_max_chars = 30\n")).input.ui_tree_max_chars == 30


def test_image_mode_keeps_ui_dup_requires(env):
    body = ('[dedup]\nui_dup_requires = "tree"\nminhash_threshold = 0.5\nminhash_num_perm = 64\n'
            "ngram = 3\nbounds_quantize_px = 2\n")
    dedup = env.load(env.project(body=body)).dedup
    assert (dedup.ui_dup_requires, dedup.minhash_threshold, dedup.ngram) == ("tree", 0.5, 3)


def test_rule79_meta_mode_none_rejected(env):
    assert env.errors(env.project(output='meta_mode = "none"\n')) == [
        f'{env.fp}:[output].meta_mode: must not be "none" when segment.mode = "image" - candidates and '
        'original polygons are carried only by _meta.regions (sidecar is legal), got "none"']


def test_rule79_sidecar_allowed(env):
    cfg = env.load(env.project(output='meta_mode = "sidecar"\n'))
    assert cfg.output.meta_mode == "sidecar"
    assert cfg.paths.sidecar is not None


def test_rule80_empty_rubric_rejected(env):
    expected = (f'{env.fp}:[quality].rubric: segment.mode = "image" has no default rubric; set '
                "quality.rubric or quality.enabled = false")
    assert env.errors(env.project(quality=None)) == [expected]
    assert env.errors(env.project(quality='mode = "pointwise"\n')) == [expected]
    assert env.load(env.project(quality='rubric = "default:ui"\n')).quality.rubric == "default:ui"
    assert env.load(env.project(quality=QUALITY_OFF)).quality.enabled is False
    # 流模式仍解析出轨迹准则；图片模式关闭 quality 时选择器不会变成轨迹准则。
    assert env.load(env.project(SEQUENCE, quality=None)).quality.rubric == "default:trajectory"
    assert env.load(env.project(quality=QUALITY_OFF)).quality.rubric == "default:ui"


# ── D9 / D15 与既有规则 ─────────────────────────────────────────────────────


def test_segment_only_allowed_with_instruction(env):
    cfg = env.load(env.project(annotate="enabled = false\n"))
    assert cfg.annotate.enabled is False and cfg.quality.enabled is False
    assert cfg.segment.llm_referenced is True
    assert referenced_profiles(cfg)[0] == ["default"]


def test_boxes_only_requires_annotate(env):
    rule29 = (f'{env.fp}:[segment].enabled: segment.mode = "image" without instruction requires '
              "annotate.enabled = true (box_parser-only runs make no model call)")
    rule17 = (f"{env.fp}:[quality].enabled: quality and annotate must not both be disabled "
              "(at least one must be enabled, 2.3.1 constraint ①)")
    assert sorted(env.errors(env.project(BOXES, annotate="enabled = false\n"))) == sorted([rule29, rule17])
    assert env.errors(env.project(BOXES, annotate="enabled = false\n",
                                  quality='rubric = "default:ui"\n')) == [rule29]


def test_sequence_still_requires_annotate(env):
    errors = env.errors(env.project(SEQUENCE, annotate="enabled = false\n", quality=None))
    assert errors == [f"{env.fp}:[segment].enabled: segment.enabled = true requires annotate.enabled = "
                      "true (constraint ⑭: episodes must be annotated into the user schema)"]
    # rule 17 对非图片工程不放宽。
    errors = env.errors(env.project(None, annotate="enabled = false\n"))
    assert errors == [f"{env.fp}:[quality].enabled: quality and annotate must not both be disabled "
                      "(at least one must be enabled, 2.3.1 constraint ①)"]


@pytest.mark.parametrize("segment", [IMAGE, BOXES, SEQUENCE], ids=["image", "boxes", "sequence"])
def test_rule29_process_and_no_generate_in_both_modes(env, segment):
    errors = env.errors(env.project(segment, run='mode = "generate_only"\n'))
    assert f'{env.fp}:[segment].enabled: segment.enabled = true requires run.mode = "process", ' \
           'got "generate_only"' in errors
    errors = env.errors(env.project(segment, body='[generate]\nenabled = true\ninstruction = "生成"\n'))
    assert (f"{env.fp}:[segment].enabled: segment.enabled = true and generate.enabled = true are mutually "
            "exclusive (stream mode does no generation)") in errors


def test_repair_attempts_above_two_warns(env, capsys):
    warning = (f'{env.fp}:[output].max_repair_attempts: segment.mode = "image" repairs segmentation at '
               "most 2 rounds; larger values do not extend it")
    env.load(env.project(output="max_repair_attempts = 3\n"))
    assert f"warning: {warning}" in capsys.readouterr().err
    env.load(env.project(output="max_repair_attempts = 2\n"))
    assert "max_repair_attempts" not in capsys.readouterr().err
    env.load(env.project(BOXES, output="max_repair_attempts = 3\n"))
    assert "max_repair_attempts" not in capsys.readouterr().err
    env.load(env.project(SEQUENCE, output="max_repair_attempts = 3\n"))
    assert "max_repair_attempts" not in capsys.readouterr().err


# ── 配置侧重键 ──────────────────────────────────────────────────────────────


def test_vision_resolved_is_evaluated_by_mode(env):
    assert env.load(env.project(IMAGE)).segment.vision_resolved is True
    assert env.load(env.project(BOXES)).segment.vision_resolved is False
    assert env.load(env.project(SEQUENCE)).segment.vision_resolved is True
    rules = SEQUENCE + 'strategy = "rules"\nnoise_filter = false\n'
    assert env.load(env.project(rules)).segment.vision_resolved is False
    assert env.load(env.project('enabled = false\nmode = "image"\ninstruction = "x"\n')).segment.vision_resolved is False


def test_image_static_budget_uses_the_image_head(env):
    instruction = "候选" * 30000
    expected = budget.TEMPLATE_HEAD_TOKENS["segment_image"] + budget.est_text(instruction)
    project = env.project(f'enabled = true\nmode = "image"\nllm = "tiny"\ninstruction = "{instruction}"\n')
    errors = env.errors(project)
    assert len(errors) == 1
    assert errors[0].startswith(f"{env.fp}:[segment]: static system-side prompt parts estimated at {expected} tokens")
    # 只用参考框时不估 segment：tiny 不被引用，也不会有预算错误。
    env.load(env.project(BOXES))


def test_segment_image_head_constant_is_registered():
    assert budget.TEMPLATE_HEAD_TOKENS["segment_image"] == 410
    assert budget.TEMPLATE_HEAD_TOKENS["segment"] == 484


def test_image_mode_emits_no_stream_warnings(env, capsys):
    env.load(env.project(quality='rubric = "default:trajectory"\n'))
    err = capsys.readouterr().err
    assert "segment.enabled = true and extract.enabled = false" not in err
    assert "segment.enabled = false" not in err


# ── rule 81：x-labelkit-region-ref ──────────────────────────────────────────

REF = {"type": "string", "x-labelkit-region-ref": True}
POST_HOOK = '''

def post(candidate, context):
    return candidate
'''


def schema_with(items: dict, **extra) -> dict:
    """组装 ``{"items": [<items>]}`` 形状的用户 Schema。"""
    return {"type": "object", "additionalProperties": False, "required": ["items"],
            "properties": {"items": {"type": "array", "items": items}}, **extra}


def region_item(**properties) -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["region_id"],
            "properties": {"region_id": REF, **properties}}


def swap_schema(project: str, schema: dict) -> str:
    """把工程文本的 schema_inline 换成给定 Schema。"""
    return project[:project.rindex("schema_inline")] + f"schema_inline = '''{json.dumps(schema)}'''\n"


def test_rule81_paths_are_frozen_and_marker_stays_in_full_schema(env):
    schema = schema_with(region_item(others={"type": "array", "items": REF},
                                     maybe={"type": ["string", "null"], "x-labelkit-region-ref": True}))
    cfg = env.load(swap_schema(env.project(), schema))
    assert cfg.region_ref_paths == ("/items/*/region_id", "/items/*/others/*", "/items/*/maybe")
    full = cfg.user_schema["properties"]["items"]["items"]["properties"]
    assert full["region_id"]["x-labelkit-region-ref"] is True
    assert "x-labelkit-region-ref" not in json.dumps(cfg.model_user_schema, default=dict)
    model = cfg.model_user_schema["properties"]["items"]["items"]["properties"]
    assert dict(model["region_id"]) == {"type": "string"}
    assert list(model) == ["region_id", "others", "maybe"]


def test_rule81_no_marker_keeps_model_schema_identical(env):
    cfg = env.load()
    assert cfg.region_ref_paths == ()
    assert cfg.model_user_schema == cfg.user_schema


def test_rule81_class_schema_takes_precedence_over_global(env):
    classes = ('[classify]\nenabled = true\nllm = "default"\nassignment = "single"\nfallback_class = "other"\n\n'
               '[[classify.classes]]\nname = "own"\ndescription = "d"\n\n'
               '[[classify.classes]]\nname = "other"\ndescription = "d"\n\n'
               "[class.own.annotate]\nschema_inline = '''"
               + json.dumps(schema_with({"type": "object", "properties": {"target": REF}})) + "'''\n")
    cfg = env.load(swap_schema(env.project(body=classes), schema_with(region_item())))
    assert cfg.region_ref_paths == ("/items/*/region_id",)
    assert cfg.class_views["own"].region_ref_paths == ("/items/*/target",)
    assert cfg.class_views["other"].region_ref_paths == ("/items/*/region_id",)
    for name in ("own", "other"):
        assert "x-labelkit-region-ref" not in json.dumps(cfg.class_views[name].model_schema, default=dict)
    assert cfg.class_views["own"].schema["properties"]["items"]["items"]["properties"]["target"][
        "x-labelkit-region-ref"] is True


@pytest.mark.parametrize("segment", [
    None,
    SEQUENCE,
    'enabled = false\nmode = "image"\n',
], ids=["no-segment", "sequence", "image-mode-parked"])
def test_rule81_region_ref_outside_image_rejected(env, segment):
    quality = QUALITY_OFF
    expected = f'{env.fp}:[output]: x-labelkit-region-ref requires segment.mode = "image"'
    errors = env.errors(swap_schema(env.project(segment, quality=quality), schema_with(region_item())))
    assert errors == [expected]


def test_rule81_outside_image_rejects_class_and_frame_schemas(env):
    classes = ('[classify]\nenabled = true\nllm = "default"\nassignment = "single"\nfallback_class = "other"\n\n'
               '[[classify.classes]]\nname = "own"\ndescription = "d"\n\n'
               '[[classify.classes]]\nname = "other"\ndescription = "d"\n\n'
               "[class.own.annotate]\nschema_inline = '''" + json.dumps(schema_with(region_item())) + "'''\n")
    assert env.errors(env.project(None, quality=QUALITY_OFF, body=classes)) == [
        f'{env.fp}:[class.own.annotate]: x-labelkit-region-ref requires segment.mode = "image"']
    frame = ("[frame.annotate]\nenabled = true\nllm = \"default\"\ninstruction = \"帧\"\nschema_inline = '''"
             + json.dumps(schema_with(region_item())) + "'''\n")
    errors = env.errors(env.project(SEQUENCE, quality=QUALITY_OFF, body=frame))
    assert f'{env.fp}:[frame.annotate]: x-labelkit-region-ref requires segment.mode = "image"' in errors


def test_rule81_outside_image_rejects_generation_schema():
    collector = _Collector()
    schema = schema_with(region_item())
    check_region_refs(collector, "p.toml", "[generate]", schema, False)
    assert collector.errors == ['p.toml:[generate]: x-labelkit-region-ref requires segment.mode = "image"']
    request = _GenerationSchemaRequest(file="p.toml", section="class.a.generate", path=None,
                                       inline=json.dumps(schema), max_bytes=100_000, project_root=Path("."))
    collector = _Collector()
    _load_generation_schema(collector, request)
    assert collector.errors == ['p.toml:[class.a.generate]: x-labelkit-region-ref requires segment.mode = "image"']


@pytest.mark.parametrize("field", [
    {"x-labelkit-postprocessor": True, "type": "string", "x-labelkit-region-ref": True},
    {"x-labelkit-postprocessor": True, "type": "object",
     "properties": {"inner": REF}, "additionalProperties": False},
    {"x-labelkit-postprocessor": True, "type": "array", "items": REF},
], ids=["same-node", "nested-property", "nested-items"])
def test_rule81_region_ref_inside_postprocessor_field_rejected(env, field):
    env.tmp.joinpath("hooks.py").write_text(HOOKS + POST_HOOK, encoding="utf-8")
    project = env.project(annotate='instruction = "标注"\npostprocessor = "hooks.py:post"\n')
    schema = schema_with({"type": "object", "additionalProperties": False, "required": ["derived"],
                          "properties": {"derived": field}})
    assert env.errors(swap_schema(project, schema)) == [
        f"{env.fp}:[output]: x-labelkit-region-ref must not be used inside a x-labelkit-postprocessor field"]


@pytest.mark.parametrize("marker", [False, "true", 1, None], ids=["false", "text", "one", "null"])
def test_rule81_region_ref_value_and_type(env, marker):
    expected = [f"{env.fp}:[output]: x-labelkit-region-ref must equal true on a string property or string items"]
    bad = {"type": "string", "x-labelkit-region-ref": marker}
    assert env.errors(swap_schema(env.project(), schema_with(region_item(extra=bad)))) == expected


@pytest.mark.parametrize("node", [
    {"type": "integer", "x-labelkit-region-ref": True},
    {"type": ["null", "string"], "x-labelkit-region-ref": True},
    {"type": ["string", "integer"], "x-labelkit-region-ref": True},
    {"type": ["string", "null", "integer"], "x-labelkit-region-ref": True},
    {"x-labelkit-region-ref": True},
], ids=["integer", "null-first", "string-integer", "three-types", "no-type"])
def test_rule81_rejects_other_types(env, node):
    assert env.errors(swap_schema(env.project(), schema_with(region_item(extra=node)))) == [
        f"{env.fp}:[output]: x-labelkit-region-ref must equal true on a string property or string items"]


@pytest.mark.parametrize("schema", [
    {"type": "object", "x-labelkit-region-ref": True},
    {"type": "object", "properties": {"a": {"anyOf": [REF]}}},
    {"type": "object", "$defs": {"ref": REF}, "properties": {"a": {"$ref": "#/$defs/ref"}}},
    {"type": "object", "additionalProperties": REF},
    {"type": "object", "properties": {"a": {"type": "array", "prefixItems": [REF]}}},
], ids=["root", "any-of", "defs", "additional-properties", "prefix-items"])
def test_rule81_rejects_positions_outside_properties_and_items(env, schema):
    assert env.errors(swap_schema(env.project(), schema)) == [
        f"{env.fp}:[output]: x-labelkit-region-ref must equal true on a string property or string items"]


def test_rule81_marker_in_data_positions_is_not_an_annotation(env):
    """default、examples 与名叫注解的属性都是数据，不触发规则，也不被删除。"""
    item = region_item(**{"x-labelkit-region-ref": {"type": "string"}})
    schema = schema_with(item, default={"x-labelkit-region-ref": 3}, examples=[{"items": []}])
    cfg = env.load(swap_schema(env.project(), schema))
    assert cfg.region_ref_paths == ("/items/*/region_id",)
    assert cfg.model_user_schema["default"]["x-labelkit-region-ref"] == 3
    assert "x-labelkit-region-ref" in cfg.model_user_schema["properties"]["items"]["items"]["properties"]
