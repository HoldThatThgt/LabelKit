"""M8 中立后校验载体的形状、冻结与导入方向（SPEC-image-region-segmentation §10.4）。"""
from __future__ import annotations

import ast
import dataclasses
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Literal, get_type_hints

import pytest

from labelkit.common.contracts import generation as contracts_generation
from labelkit.common.contracts import post_validation
from labelkit.common.contracts.post_validation import (
    PostValidatedCallRequest,
    PostValidationResult,
    ValidatedPostCall,
)
from labelkit.common.contracts.types import Usage
from labelkit.common.inference import schema_engine as schema_engine_module
from labelkit.common.inference.llm_client import PromptBundle
from labelkit.common.inference.schema_engine import CallScope
from labelkit.operators.generation import scenario, state

ROOT = Path(__file__).resolve().parents[3]
LOCALNS = {"PromptBundle": PromptBundle, "CallScope": CallScope, "Usage": Usage}
JSON_OBJECT = Mapping[str, object]
MANIFEST = {
    PostValidationResult: {
        "violations": tuple[str, ...],
        "proof": object | None,
    },
    PostValidatedCallRequest: {
        "profile": str,
        "prompt": PromptBundle,
        "schema": JSON_OBJECT,
        "scope": CallScope,
        "post_validator": Callable[[JSON_OBJECT], PostValidationResult],
    },
    ValidatedPostCall: {
        "object": JSON_OBJECT,
        "proof": object,
        "resolved_at": Literal["l0_or_clean", "l1", "l3_1", "l3_2"],
        "usage": Usage,
        "attempts": int,
        "model": str,
    },
}


@pytest.mark.parametrize(("carrier", "expected"), MANIFEST.items(), ids=lambda value: getattr(value, "__name__", ""))
def test_carrier_fields_and_annotations_are_exact(carrier, expected):
    fields = dataclasses.fields(carrier)
    assert tuple(item.name for item in fields) == tuple(expected)
    assert all(item.default is dataclasses.MISSING for item in fields)
    assert all(item.default_factory is dataclasses.MISSING for item in fields)
    assert carrier.__dataclass_params__.frozen is True
    assert get_type_hints(carrier, localns=LOCALNS) == expected


def test_module_declares_only_the_contract_surface():
    declared = {
        name for name, value in vars(post_validation).items()
        if isinstance(value, type) and value.__module__ == post_validation.__name__
    }
    assert declared == {"PostValidationResult", "PostValidatedCallRequest", "ValidatedPostCall"}
    assert post_validation.CallPostValidator == Callable[[JSON_OBJECT], PostValidationResult]


def test_module_has_no_runtime_inference_imports():
    """运行期完全不导入 labelkit；三个类型引用都只在 TYPE_CHECKING 块内。"""
    tree = ast.parse(Path(post_validation.__file__).read_text(encoding="utf-8"))
    runtime_imports, guarded_imports = set(), set()
    for node in tree.body:
        if isinstance(node, ast.If) and getattr(node.test, "id", "") == "TYPE_CHECKING":
            guarded_imports.update(item.module for item in node.body)
        elif isinstance(node, ast.ImportFrom):
            runtime_imports.add(node.module)
        elif isinstance(node, ast.Import):
            runtime_imports.update(alias.name for alias in node.names)
    assert not {name for name in runtime_imports if name.startswith("labelkit")}
    assert guarded_imports == {
        "labelkit.common.contracts.types",
        "labelkit.common.inference.llm_client",
        "labelkit.common.inference.schema_engine",
    }
    probe = (
        "import sys; import labelkit.common.contracts.post_validation; "
        "assert not [m for m in sys.modules if m.startswith('labelkit.common.inference')]; "
        "assert 'labelkit.common.contracts.generation' not in sys.modules"
    )
    done = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("name", [
    "PostValidationResult", "CallPostValidator", "PostValidatedCallRequest",
    "ValidatedPostCall", "ValidatedGenerationCall",
])
def test_generation_module_keeps_no_alias_or_reexport(name):
    assert name not in vars(contracts_generation)


def test_old_names_are_gone_and_every_caller_shares_one_class():
    assert not hasattr(schema_engine_module, "ValidatedGenerationCall")
    assert not hasattr(post_validation, "ValidatedGenerationCall")
    assert "event_execution" not in {item.name for item in dataclasses.fields(PostValidationResult)}
    assert "event_execution" not in {item.name for item in dataclasses.fields(ValidatedPostCall)}
    assert schema_engine_module.PostValidatedCallRequest is PostValidatedCallRequest
    assert schema_engine_module.ValidatedPostCall is ValidatedPostCall
    assert schema_engine_module.PostValidationResult is PostValidationResult
    assert scenario.PostValidatedCallRequest is PostValidatedCallRequest
    assert state.PostValidationResult is PostValidationResult


def test_request_schema_is_deep_frozen_and_isolated_from_the_caller():
    schema = {"properties": {"plan": {"required": ["n"]}}, "enum": {"a"}}
    request = PostValidatedCallRequest(
        "profile", PromptBundle(messages=()), schema, CallScope(), lambda _candidate: None)
    schema["properties"]["plan"]["required"].append("changed")
    assert isinstance(request.schema, MappingProxyType)
    assert isinstance(request.schema["properties"]["plan"], MappingProxyType)
    assert request.schema["properties"]["plan"]["required"] == ("n",)
    assert request.schema["enum"] == frozenset({"a"})
    with pytest.raises(TypeError):
        request.schema["properties"] = {}


def test_validated_object_is_deep_frozen_while_proof_is_never_copied():
    proof = {"mutable": [1, 2]}
    candidate = {"regions": [{"box_id": "n1"}]}
    result = ValidatedPostCall(candidate, proof, "l0_or_clean", Usage(1, 2), 1, "m")
    candidate["regions"][0]["box_id"] = "changed"
    assert result.object["regions"][0]["box_id"] == "n1"
    assert isinstance(result.object["regions"], tuple)
    assert result.proof is proof
    assert type(result.proof) is dict
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.proof = None


def test_result_keeps_proof_instance_and_normalizes_only_violation_containers():
    proof = {"mutable": [1]}
    assert PostValidationResult((), proof).proof is proof
    assert PostValidationResult(["a", "b"], None).violations == ("a", "b")
    assert PostValidationResult("bad", None).violations == "bad"
