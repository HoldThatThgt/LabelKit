"""Regression coverage for the frozen legacy import compatibility surface."""
from __future__ import annotations

from importlib import import_module

import pytest


CORE_OBJECTS = (
    ("labelkit.cli", "labelkit.cli.main", "main"),
    ("labelkit.types", "labelkit.common.contracts.types", "Record"),
    ("labelkit.stage", "labelkit.common.contracts.stage", "RunContext"),
    ("labelkit.errors", "labelkit.common.errors", "LabelKitError"),
    ("labelkit.config", "labelkit.common.config", "ResolvedConfig"),
    ("labelkit.config.model", "labelkit.common.config.model", "ResolvedConfig"),
    ("labelkit.config.loader", "labelkit.common.config.loader", "load"),
    ("labelkit.llm_client", "labelkit.common.runtime.llm_client", "LLMClient"),
    ("labelkit.schema_engine", "labelkit.common.runtime.schema_engine", "SchemaEngine"),
    ("labelkit.obslog", "labelkit.common.observability.obslog", "EventLog"),
    ("labelkit.hooks", "labelkit.common.extensions.hooks", "resolve_hook"),
    ("labelkit.ingest", "labelkit.operators.ingest", "Ingestor"),
    ("labelkit.segment", "labelkit.operators.segment", "SegmentStage"),
    ("labelkit.dedup", "labelkit.operators.dedup", "DedupStage"),
    ("labelkit.classify", "labelkit.operators.classify", "ClassifyStage"),
    ("labelkit.extract", "labelkit.operators.extract", "ExtractStage"),
    ("labelkit.quality", "labelkit.operators.quality", "QualityStage"),
    ("labelkit.generate", "labelkit.operators.generate", "GenerateStage"),
    ("labelkit.annotate", "labelkit.operators.annotate", "AnnotateStage"),
    ("labelkit.verify", "labelkit.operators.verify", "VerifyStage"),
    ("labelkit.emitter", "labelkit.operators.emitter", "Emitter"),
    ("labelkit.orchestrator", "labelkit.orchestration.orchestrator", "Orchestrator"),
)


@pytest.mark.parametrize(("legacy_path", "canonical_path", "symbol"), CORE_OBJECTS)
def test_legacy_core_object_identity(legacy_path, canonical_path, symbol):
    legacy_module = import_module(legacy_path)
    canonical_module = import_module(canonical_path)

    assert getattr(legacy_module, symbol) is getattr(canonical_module, symbol)


DIRECT_CALL_SURFACES = (
    ("labelkit.annotate", "labelkit.operators.annotate", "annotate_record"),
    ("labelkit.annotate", "labelkit.operators.annotate", "build_annotate_prompt"),
    ("labelkit.classify", "labelkit.operators.classify", "build_classify_prompt"),
    ("labelkit.generate", "labelkit.operators.generate", "build_generate_prompt"),
    ("labelkit.segment", "labelkit.operators.segment", "judge_window"),
    ("labelkit.extract", "labelkit.operators.extract", "extract_transition"),
    ("labelkit.verify", "labelkit.operators.verify", "build_verify_prompt"),
    ("labelkit.stage", "labelkit.common.contracts.stage", "RunContext"),
    ("labelkit.llm_client", "labelkit.common.runtime.llm_client", "LLMClient"),
    ("labelkit.schema_engine", "labelkit.common.runtime.schema_engine", "SchemaEngine"),
)


@pytest.mark.parametrize(
    ("legacy_path", "canonical_path", "symbol"), DIRECT_CALL_SURFACES
)
def test_legacy_direct_call_surface_identity(legacy_path, canonical_path, symbol):
    legacy_surface = getattr(import_module(legacy_path), symbol)
    canonical_surface = getattr(import_module(canonical_path), symbol)

    assert legacy_surface is canonical_surface
    assert callable(legacy_surface)
