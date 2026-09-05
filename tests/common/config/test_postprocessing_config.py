"""后处理配置公开 loader 的启动、继承、冻结与提示词投影契约。"""
import json

import pytest

from labelkit.common.config import load
from labelkit.common.config import _constraints as config_constraints
from labelkit.common.config.model import CliOverrides
from labelkit.common.errors import ConfigError
from labelkit.common.extensions import hooks as hook_module
from labelkit.common.extensions.postprocessing import project_postprocessor_schema
from tests.common.config.test_config import CLASSIFY_BODY, FRAME_CLASSIFY_ONLY, SEG_ON, Env, _cw_config
from tests.common.config.test_temporal_config import _complete_temporal_project

MARKER = "x-labelkit-postprocessor"
HOOK_SOURCE = """
CALLS = 0

def complete(obj, record=None):
    global CALLS
    CALLS += 1
    raise AssertionError("business hook must not run in config")

def other(obj, record):
    global CALLS
    CALLS += 1
    return obj
"""


def _schema():
    return {
        "type": "object",
        "properties": {"value": {"type": "string"}, "length": {"type": "integer", "minimum": 0, MARKER: True}},
        "required": ["value", "length"],
        "additionalProperties": False,
    }


@pytest.fixture
def env(tmp_path):
    (tmp_path / "hooks.py").write_text(HOOK_SOURCE, encoding="utf-8")
    return Env(tmp_path)


def _annotation(extra="", examples=True):
    body = 'instruction = "label"\npostprocessor = "hooks.py:complete"\n' + extra
    if examples:
        body += '\nexamples = [{ input = "hi", output = { value = "hi", length = 2 } }]\n'
    return body


def _frame(extra="", examples=True):
    body = '[frame.annotate]\nenabled = true\n' + _annotation(extra, examples)
    return body + "schema_inline = '''\n" + json.dumps(_schema()) + "\n'''\n"


def _load_code_project(env, **kwargs):
    kwargs.setdefault("schema", json.dumps(_schema()))
    kwargs.setdefault("annotate_body", _annotation())
    return env.load(project_text=env.project(**kwargs))


def test_global_hook_is_frozen_project_relative_without_any_startup_business_call(env, monkeypatch):
    cfg = env.load(project_text=env.project(schema=json.dumps(_schema()), annotate_body=_annotation()),
                   cli=CliOverrides(dry_run=True))
    hook = cfg.annotate.resolved_postprocessor
    assert hook.reference == str(env.tmp / "hooks.py") + ":complete"
    assert hook.target.__globals__["CALLS"] == 0
    assert "length" in cfg.user_schema["properties"]
    assert "length" not in cfg.model_user_schema["properties"]
    assert cfg.annotate.examples[0].output == {"value": "hi"}
    with pytest.raises(TypeError):
        cfg.model_user_schema["properties"]["value"]["type"] = "integer"
    with pytest.raises(TypeError):
        cfg.annotate.examples[0].output["value"] = "changed"
    outside = env.tmp / "elsewhere"
    outside.mkdir()
    monkeypatch.chdir(outside)
    reloaded = load(env.tmp / "config.toml", env.tmp / "project.toml", CliOverrides())
    assert reloaded.annotate.resolved_postprocessor.reference == hook.reference
    assert reloaded.annotate.resolved_postprocessor.target.__globals__["CALLS"] == 0


def test_global_full_schemas_are_deeply_frozen_and_isolated_from_loader_sources(env, monkeypatch):
    sources = {}
    load_user = config_constraints._load_user_schema
    load_frame = config_constraints._load_frame_schema
    def user(*args):
        result = load_user(*args)
        sources["user"] = result[0]
        return result
    def frame(*args):
        result = load_frame(*args)
        sources["frame"] = result[0]
        return result
    monkeypatch.setattr(config_constraints, "_load_user_schema", user)
    monkeypatch.setattr(config_constraints, "_load_frame_schema", frame)
    schema = _schema()
    schema["properties"]["value"]["enum"] = ["hi"]
    frame_body = _frame().replace(json.dumps(_schema()), json.dumps(schema))
    cfg = _load_code_project(env, schema=json.dumps(schema), body=SEG_ON + frame_body)
    for name, frozen in (("user", cfg.user_schema), ("frame", cfg.frame_schema)):
        assert frozen is not sources[name]
        assert frozen["properties"]["value"]["enum"] == ("hi",)
        with pytest.raises(TypeError):
            frozen["properties"]["value"]["type"] = "integer"
        with pytest.raises(AttributeError):
            frozen["required"].append("extra")
        sources[name]["properties"]["value"]["type"] = "integer"
        sources[name]["properties"]["value"]["enum"].append("later")
        sources[name]["required"].append("extra")
        assert frozen["properties"]["value"]["type"] == "string"
        assert frozen["properties"]["value"]["enum"] == ("hi",)
        assert frozen["required"] == ("value", "length")


def test_record_class_override_and_inheritance_keep_reference_and_target_together(env):
    body = CLASSIFY_BODY + '\n[class.qa.annotate]\npostprocessor = "hooks.py:other"\n'
    cfg = _load_code_project(env, body=body)
    global_hook = cfg.annotate.resolved_postprocessor
    override = cfg.class_views["qa"].annotate.resolved_postprocessor
    assert override.reference.endswith(":other") and override.target.__name__ == "other"
    assert cfg.class_views["writing"].annotate.resolved_postprocessor is global_hook
    for view in cfg.class_views.values():
        assert "length" not in view.model_schema["properties"]
        assert view.annotate.examples[0].output == {"value": "hi"}
        assert view.annotate.resolved_postprocessor.target.__globals__["CALLS"] == 0


def test_record_class_can_own_schema_and_hook_without_global_postprocessor(env):
    body = CLASSIFY_BODY + "\n[class.qa.annotate]\n" + _annotation()
    body += "schema_inline = '''\n" + json.dumps(_schema()) + "\n'''\n"
    cfg = env.load(project_text=env.project(body=body))
    assert cfg.annotate.resolved_postprocessor is None
    view = cfg.class_views["qa"]
    assert view.annotate.resolved_postprocessor.reference.endswith(":complete")
    assert "length" not in view.model_schema["properties"]
    assert cfg.class_views["writing"].model_schema == cfg.model_user_schema


def test_record_class_full_schema_is_deeply_frozen(env):
    schema = _schema()
    schema["properties"]["value"]["enum"] = ["hi"]
    body = CLASSIFY_BODY + "\n[class.qa.annotate]\n" + _annotation()
    body += "schema_inline = '''\n" + json.dumps(schema) + "\n'''\n"
    cfg = env.load(project_text=env.project(body=body))
    full = cfg.class_views["qa"].schema
    assert full["properties"]["value"]["enum"] == ("hi",)
    with pytest.raises(TypeError):
        full["properties"]["value"]["type"] = "integer"
    with pytest.raises(AttributeError):
        full["properties"]["value"]["enum"].append("changed")
    with pytest.raises(AttributeError):
        full["required"].append("changed")
    assert full["required"] == ("value", "length")
    assert full["properties"]["length"][MARKER] is True


def test_frame_global_and_class_overrides_freeze_shared_projection(env):
    body = SEG_ON + FRAME_CLASSIFY_ONLY + _frame()
    body += '\n[frame.class.task_request.annotate]\npostprocessor = "hooks.py:other"\n'
    body += '\n[frame.class.chitchat.annotate]\nenabled = false\n'
    cfg = env.load(project_text=env.project(body=body))
    base = cfg.frame_annotate.resolved_postprocessor
    override = cfg.frame_class_views["task_request"].resolved_postprocessor
    assert base.reference.endswith(":complete")
    assert override.reference.endswith(":other") and override.target.__name__ == "other"
    assert cfg.frame_class_views["other"].resolved_postprocessor is base
    assert cfg.frame_class_views["chitchat"].enabled is False
    assert "length" in cfg.frame_schema["properties"]
    assert "length" not in cfg.model_frame_schema["properties"]
    assert cfg.frame_annotate.examples[0].output == {"value": "hi"}
    for view in cfg.frame_class_views.values():
        assert view.examples[0].output == {"value": "hi"}
        assert view.resolved_postprocessor.target.__globals__["CALLS"] == 0
    with pytest.raises(TypeError):
        cfg.model_frame_schema["properties"]["value"]["type"] = "integer"


@pytest.mark.parametrize("location", ["global", "class", "frame", "frame_class"])
def test_fewshot_expected_output_must_include_required_code_fields(env, location):
    missing = 'examples = [{ input = "hi", output = { value = "hi" } }]\n'
    body = ""
    annotate = _annotation(examples=False)
    if location == "global":
        annotate += missing
    elif location == "class":
        body = CLASSIFY_BODY + "\n[class.qa.annotate]\n" + missing
    elif location == "frame":
        body = SEG_ON + _frame(examples=False) + missing
    else:
        body = SEG_ON + FRAME_CLASSIFY_ONLY + _frame(examples=False)
        body += "\n[frame.class.task_request.annotate]\n" + missing
    errors = env.errors(project_text=env.project(body=body, annotate_body=annotate, schema=json.dumps(_schema())))
    assert any("length" in error and "required" in error for error in errors), errors


@pytest.mark.parametrize("location", ["global", "class", "frame", "frame_class"])
def test_fewshot_code_owned_values_must_satisfy_final_constraints(env, location):
    invalid = 'examples = [{ input = "hi", output = { value = "hi", length = -1 } }]\n'
    body = ""
    annotate = _annotation(examples=False)
    if location == "global":
        annotate += invalid
    elif location == "class":
        body = CLASSIFY_BODY + "\n[class.qa.annotate]\n" + invalid
    elif location == "frame":
        body = SEG_ON + _frame(examples=False) + invalid
    else:
        body = SEG_ON + FRAME_CLASSIFY_ONLY + _frame(examples=False)
        body += "\n[frame.class.task_request.annotate]\n" + invalid
    project = env.project(body=body, annotate_body=annotate, schema=json.dumps(_schema()))
    errors = env.errors(project_text=project)
    sections = {
        "global": "annotate.examples",
        "class": "class.qa.annotate.examples",
        "frame": "frame.annotate.examples",
        "frame_class": "frame.class.task_request.annotate.examples",
    }
    assert any(
        f"[[{sections[location]}]][1].output" in error
        and "less than the minimum of 0" in error
        for error in errors
    ), errors


@pytest.mark.parametrize("location", ["global", "class", "frame", "frame_class"])
@pytest.mark.parametrize("key", ["resolved_postprocessor", "model_user_schema", "model_frame_schema"])
def test_internal_frozen_fields_are_errors_in_every_annotation_namespace(env, location, key):
    bad = f'{key} = "forged"\n'
    body = ""
    annotate = _annotation()
    if location == "global":
        annotate += bad
    elif location == "class":
        body = CLASSIFY_BODY + "\n[class.qa.annotate]\n" + bad
    elif location == "frame":
        body = SEG_ON + _frame(bad)
    else:
        body = SEG_ON + FRAME_CLASSIFY_ONLY + _frame()
        body += "\n[frame.class.task_request.annotate]\n" + bad
    errors = env.errors(project_text=env.project(body=body, annotate_body=annotate, schema=json.dumps(_schema())))
    assert any(key in error and "internal" in error for error in errors), errors


@pytest.mark.parametrize("location", ["global", "class", "frame", "frame_class"])
def test_disabled_configured_references_still_require_static_validation(env, location):
    missing = 'postprocessor = "missing.py:complete"\n'
    annotate = 'instruction = "label"\n'
    if location == "global":
        annotate += "enabled = false\n" + missing
        body = ""
    elif location == "class":
        body = "\n[class.qa.annotate]\n" + missing
    elif location == "frame":
        body = "\n[frame.annotate]\nenabled = false\n" + missing
    else:
        body = SEG_ON + FRAME_CLASSIFY_ONLY + _frame()
        body += "\n[frame.class.task_request.annotate]\nenabled = false\n" + missing
    errors = env.errors(project_text=env.project(body=body, annotate_body=annotate))
    assert any("cannot load postprocessor" in error for error in errors), errors


@pytest.mark.parametrize("raw", ['""', "false", "42"])
def test_postprocessor_reference_cannot_be_empty_or_disable_sentinel(env, raw):
    errors = env.errors(project_text=env.project(annotate_body=f'instruction = "label"\npostprocessor = {raw}'))
    assert any("postprocessor" in error for error in errors)


def test_output_alias_and_root_internal_schema_fields_are_rejected(env):
    project = env.project().replace("[output]", '[output]\npostprocessor = "hooks.py:complete"')
    project = project.replace("schema_version = 1", 'schema_version = 1\nmodel_user_schema = "forged"', 1)
    errors = env.errors(project_text=project)
    assert any("[output].postprocessor" in error for error in errors)
    assert any("model_user_schema" in error and "internal" in error for error in errors)


def test_code_owned_schema_requires_effective_hook_and_plain_schema_stays_open(env):
    errors = env.errors(project_text=env.project(schema=json.dumps(_schema())))
    assert any("postprocessor" in error and "required" in error for error in errors)
    schema = {"type": "object", "properties": {"value": {"anyOf": [{"type": "string"}, {"type": "null"}]}}}
    cfg = _load_code_project(env, schema=json.dumps(schema), annotate_body=_annotation(examples=False))
    assert project_postprocessor_schema(cfg.model_user_schema) == schema


@pytest.mark.parametrize("location", ["class_own_schema", "class_inherited_schema", "frame", "frame_class"])
def test_enabled_class_and_frame_schemas_require_their_effective_postprocessor(env, location):
    rendered = json.dumps(_schema())
    kwargs = {}
    if location == "class_own_schema":
        body = CLASSIFY_BODY + "\n[class.qa.annotate]\nschema_inline = '''\n" + rendered + "\n'''\n"
        expected = "[class.qa.annotate].postprocessor"
    elif location == "class_inherited_schema":
        body = CLASSIFY_BODY
        for name in ("writing", "other"):
            body += f'\n[class.{name}.annotate]\npostprocessor = "hooks.py:complete"\n'
        kwargs["schema"] = rendered
        expected = "[class.qa.annotate].postprocessor"
    else:
        body = SEG_ON + (FRAME_CLASSIFY_ONLY if location == "frame_class" else "")
        body += _frame(examples=False).replace('postprocessor = "hooks.py:complete"\n', "")
        expected = "[frame.annotate].postprocessor"
        if location == "frame_class":
            body += '\n[frame.class.chitchat.annotate]\nenabled = false\n'
            body += '\n[frame.class.other.annotate]\npostprocessor = "hooks.py:complete"\n'
            expected = "[frame.class.task_request.annotate].postprocessor"
    errors = env.errors(project_text=env.project(body=body, **kwargs))
    assert len(errors) == 1
    assert expected in errors[0] and "required for code-owned annotation fields" in errors[0]


def test_schema_and_hook_errors_are_aggregated(env):
    schema = _schema()
    del schema["additionalProperties"]
    annotate = _annotation(examples=False).replace("hooks.py", "missing.py")
    errors = env.errors(project_text=env.project(schema=json.dumps(schema), annotate_body=annotate))
    assert any("additionalProperties" in error for error in errors)
    assert any("cannot load postprocessor" in error for error in errors)


@pytest.mark.parametrize("required", [1, None, True])
def test_invalid_full_schema_keeps_meta_schema_and_hook_errors_aggregated(env, required):
    schema = _schema()
    schema["required"] = required
    annotate = _annotation(examples=False).replace("hooks.py", "missing.py")
    errors = env.errors(project_text=env.project(schema=json.dumps(schema), annotate_body=annotate))
    assert any("required" in error and "schema" in error for error in errors)
    assert any("cannot load postprocessor" in error for error in errors)


def test_fewshot_validator_cannot_mutate_nested_expected_output(env):
    source = """
def validate(obj, record):
    if "nested" in obj:
        obj["nested"]["value"] = 99
    return []
"""
    (env.tmp / "validator.py").write_text(source, encoding="utf-8")
    schema = {"type": "object", "properties": {"nested": {"type": "object"}}}
    annotate = 'instruction = "label"\nexamples = [{input = "hi", output = {nested = {value = 1}}}]'
    project = env.project(schema=json.dumps(schema), annotate_body=annotate)
    project = project.replace("[output]", '[output]\nvalidator = "validator.py:validate"')
    cfg = env.load(project_text=project)
    assert cfg.annotate.examples[0].output["nested"]["value"] == 1


def test_model_budget_omits_code_owned_schema_content(env):
    schema = _schema()
    schema["properties"]["length"]["description"] = "派生" * 10000
    cfg = env.load(config_text=_cw_config(5500),
                   project_text=env.project(schema=json.dumps(schema), annotate_body=_annotation()))
    assert "length" not in cfg.model_user_schema["properties"]


def test_class_static_budget_uses_its_projected_model_schema(env):
    class_schema = _schema()
    class_schema["properties"]["length"]["description"] = "派生" * 10000

    def project(schema):
        body = CLASSIFY_BODY + "\n[class.qa.annotate]\n" + _annotation(examples=False)
        body += "schema_inline = '''\n" + json.dumps(schema) + "\n'''\n"
        return env.project(
            schema=json.dumps(_schema()),
            annotate_body=_annotation(examples=False),
            body=body,
        )

    cfg = env.load(config_text=_cw_config(5500), project_text=project(class_schema))
    view = cfg.class_views["qa"]
    assert len(view.schema["properties"]["length"]["description"]) == 20000
    assert "length" not in view.model_schema["properties"]
    assert "description" not in cfg.user_schema["properties"]["length"]

    unmarked = json.loads(json.dumps(class_schema))
    del unmarked["properties"]["length"][MARKER]
    errors = env.errors(config_text=_cw_config(5500), project_text=project(unmarked))
    assert any(
        "[annotate]: static system-side prompt parts estimated" in error
        for error in errors
    ), errors


def test_frame_static_budget_uses_its_projected_model_schema(env):
    frame_schema = _schema()
    frame_schema["properties"]["length"]["description"] = "派生" * 10000

    def project(schema):
        frame = _frame(examples=False).replace(json.dumps(_schema()), json.dumps(schema))
        return env.project(
            schema=json.dumps(_schema()),
            annotate_body=_annotation(examples=False),
            body=SEG_ON + frame,
        )

    cfg = env.load(config_text=_cw_config(12000), project_text=project(frame_schema))
    assert len(cfg.frame_schema["properties"]["length"]["description"]) == 20000
    assert "length" not in cfg.model_frame_schema["properties"]
    assert "description" not in cfg.user_schema["properties"]["length"]

    unmarked = json.loads(json.dumps(frame_schema))
    del unmarked["properties"]["length"][MARKER]
    errors = env.errors(config_text=_cw_config(12000), project_text=project(unmarked))
    assert any(
        "[frame.annotate]: static system-side prompt parts estimated" in error
        for error in errors
    ), errors


@pytest.mark.parametrize("location", ["global", "class", "frame", "frame_class"])
def test_model_budget_omits_large_code_owned_fewshot_in_every_namespace(env, location):
    schema = _schema()
    schema["properties"]["length"] = {"type": "string", MARKER: True}
    rendered = json.dumps(schema)
    example = 'examples = [{input = "hi", output = {value = "hi", length = "' + "派生" * 10000 + '"}}]\n'
    annotate, body = _annotation(examples=False), ""
    if location == "global":
        annotate += example
    elif location == "class":
        body = CLASSIFY_BODY + "\n[class.qa.annotate]\n" + example
    else:
        body = SEG_ON + (FRAME_CLASSIFY_ONLY if location == "frame_class" else "")
        body += "[frame.annotate]\nenabled = true\n" + _annotation(examples=False)
        body += "schema_inline = '''\n" + rendered + "\n'''\n"
        if location == "frame_class":
            body += "\n[frame.class.task_request.annotate]\n"
        body += example
    cfg = env.load(config_text=_cw_config(12000),
                   project_text=env.project(schema=rendered, annotate_body=annotate, body=body))
    sources = {
        "global": cfg.annotate.examples,
        "class": cfg.class_views.get("qa").annotate.examples if "qa" in cfg.class_views else (),
        "frame": cfg.frame_annotate.examples,
        "frame_class": cfg.frame_class_views.get("task_request").examples
        if "task_request" in cfg.frame_class_views else (),
    }
    assert sources[location][0].output == {"value": "hi"}


def test_fewshot_data_keys_named_internal_fields_are_not_configuration(env):
    schema = {"type": "object"}
    annotate = _annotation(examples=False)
    annotate += 'examples = [{input="hi", output={resolved_postprocessor="data", model_user_schema={value=1}}}]'
    cfg = env.load(project_text=env.project(schema=json.dumps(schema), annotate_body=annotate))
    assert cfg.annotate.examples[0].output["resolved_postprocessor"] == "data"


@pytest.mark.parametrize("prefix", ["class.qa", "frame.class.qa"])
@pytest.mark.parametrize("value", ['""', "false"])
def test_parked_class_reference_type_errors_are_aggregated(env, prefix, value):
    errors = env.errors(project_text=env.project(body=f"[{prefix}.annotate]\npostprocessor = {value}"))
    assert any("postprocessor: expected non-empty string" in error for error in errors)


@pytest.mark.parametrize("prefix", ["class", "frame.class"])
def test_unknown_class_and_explicit_hook_errors_are_both_aggregated(env, prefix):
    body = CLASSIFY_BODY if prefix == "class" else SEG_ON + FRAME_CLASSIFY_ONLY
    body += f'\n[{prefix}.unknown.annotate]\npostprocessor = "missing.py:process"\n'
    errors = env.errors(project_text=env.project(body=body))
    assert any("unknown" in error and "is not in" in error for error in errors)
    assert any("cannot load postprocessor" in error for error in errors)


@pytest.mark.parametrize("prefix", ["class", "frame.class"])
def test_unknown_class_reference_is_loaded_once_without_executing_business_hook(env, prefix, monkeypatch):
    imported = []
    original = hook_module._load_module
    def observe(path):
        module = original(path)
        imported.append(module)
        return module
    monkeypatch.setattr(hook_module, "_load_module", observe)
    if prefix == "class":
        annotate = _annotation(examples=False)
        body = CLASSIFY_BODY + '\n[class.qa.annotate]\npostprocessor = "hooks.py:other"\n'
    else:
        annotate = 'instruction = "label"'
        body = SEG_ON + FRAME_CLASSIFY_ONLY + _frame(examples=False)
        body += '\n[frame.class.task_request.annotate]\npostprocessor = "hooks.py:other"\n'
    body += f'\n[{prefix}.unknown.annotate]\npostprocessor = "hooks.py:complete"\n'
    errors = env.errors(project_text=env.project(body=body, annotate_body=annotate))
    assert len(errors) == 1 and "is not in" in errors[0]
    assert len(imported) == 3
    assert all(module.CALLS == 0 for module in imported)


def _temporal_with_code(tmp_path):
    root, project = _complete_temporal_project(tmp_path)
    (root / "out").mkdir(exist_ok=True)
    hook_file = root / "hooks.py"
    hook_file.write_text(hook_file.read_text(encoding="utf-8") + HOOK_SOURCE, encoding="utf-8")
    target = root / "schemas" / "annotation.json"
    schema = json.loads(target.read_text(encoding="utf-8"))
    schema["properties"]["length"] = {"type": "integer", MARKER: True}
    schema["required"].append("length")
    target.write_text(json.dumps(schema), encoding="utf-8")
    text = project.read_text(encoding="utf-8")
    anchor = 'schema_path = "schemas/annotation.json"'
    position = text.rfind(anchor) + len(anchor)
    text = text[:position] + '\npostprocessor = "hooks.py:complete"' + text[position:]
    example = """[[class.ticket_booking.annotate.examples]]
input = "book"
output = { intent = "book", outcome = "ticketed", request_id = "R-1", ticket_id = "T-1", summary = "done", length = 4 }

"""
    text = text.replace("[frame.class.task_request]", example + "[frame.class.task_request]", 1)
    project.write_text(text, encoding="utf-8")
    return root, project


def test_temporal_fewshot_omits_framework_time_but_requires_code_fields(tmp_path):
    root, project = _temporal_with_code(tmp_path)
    cfg = load(root / "config.toml", project, CliOverrides())
    view = cfg.class_views["ticket_booking"]
    assert "timestamp" not in view.model_schema["properties"]
    assert "length" not in view.model_schema["properties"]
    assert "timestamp" not in view.annotate.examples[0].output
    assert "length" not in view.annotate.examples[0].output
    assert view.annotate.resolved_postprocessor.target.__globals__["CALLS"] == 0
    text = project.read_text(encoding="utf-8").replace(", length = 4", "")
    project.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError) as captured:
        load(root / "config.toml", project, CliOverrides())
    assert any("length" in error and "required" in error for error in captured.value.errors)


def test_code_time_target_overlap_is_rejected_before_time_projection(tmp_path):
    root, project = _complete_temporal_project(tmp_path)
    target = root / "schemas" / "annotation.json"
    schema = json.loads(target.read_text(encoding="utf-8"))
    schema["properties"]["timestamp"][MARKER] = True
    target.write_text(json.dumps(schema), encoding="utf-8")
    with pytest.raises(ConfigError) as captured:
        load(root / "config.toml", project, CliOverrides())
    assert any("overlap business time" in error for error in captured.value.errors)
