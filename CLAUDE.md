# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository status

Implementation in progress from the design spec:

- `labelkit-design-v1.html` / `.pdf` — the authoritative product design specification (v1.4, in Chinese). Implementation-level: every design decision is already made; implement from it rather than re-deciding.
- `spec/*.md` — the same spec extracted to markdown, split by section (`301-m1-config.md` … `312-m12-logging.md` per module; `40-ch4-data-structures.md`, `50-ch5-config-spec.md`, `60-ch6-io-formats.md`, `70-ch7-logging.md`, `90-appendix-a-rubrics.md`). **Read these instead of the HTML.**
- `docs/CONTRACTS.md` — frozen cross-module interface contract (signatures, config dataclasses, event names, prompt templates). Implementation must match it; deviations require updating it.
- `labelkit/` — the package; `tests/` — pytest suite.

## Commands

```bash
uv sync                          # create venv (Python 3.12) + install deps
uv run pytest -q -m 'not integration'         # offline suite (no network)
uv run pytest tests/integration -q -m integration  # real-LLM tests (needs .env)
uv run pytest tests/test_dedup.py::test_name -q    # run a single test
uv run labelkit --help           # run the CLI
```

## Real-LLM testing (no mocks — user directive)

Mock LLM servers/transports are forbidden. LLM-dependent behavior is tested against the real endpoint `https://api.z.ai/api/anthropic` (provider `anthropic`), model **`glm-5.2` only**. The key lives in the git-ignored `.env` as `LABELKIT_ZAI_KEY`; `tests/conftest.py` auto-loads it and auto-skips `integration`-marked tests when absent. Offline unit tests cover pure logic only (parsing, hashing, BT fitting, prompt assembly, validation).

End-to-end example projects (run from their directory, `set -a && source ../../.env && set +a` first):

```bash
cd examples/text     && mkdir -p out && uv run labelkit run --config ../config.toml --project project.toml
cd examples/ui       && mkdir -p out && uv run labelkit run --config ../config.toml --project project.toml
cd examples/generate && mkdir -p out && uv run labelkit run --config ../config.toml --project project.toml
```

`examples/text` exercises dedup + pointwise gate + annotate; `examples/ui` exercises pairing, pHash/tree dedup, pairwise QuRating, vision annotation, verify+repair; `examples/generate` exercises `generate_only` mode. Fixtures include deliberate duplicates, junk records, a cross-subdirectory UI pair, and an orphan tree — don't "clean them up".

## What LabelKit is

A **single-machine, single-process, stateless Python CLI batch tool** that runs collected data (plain-text JSONL, or screenshot + UI-tree file pairs) through a configurable LLM-powered pipeline: dedup → quality scoring (QuRating) → auto-annotation → optional generation and LLM-as-a-Judge verification. Output is JSONL whose structure is user-defined via JSON Schema and guaranteed by a code-side schema engine. A `generate_only` mode (v1.4) synthesizes a dataset from scratch with no input data.

## Binding technical decisions (from spec §1.6, §2.6)

- **Python ≥ 3.11** (uses stdlib `tomllib`). Third-party deps are limited to: `httpx` (async HTTP), `jsonschema` (validation), `datasketch` (MinHash-LSH), `Pillow` + `imagehash` (pHash), `json-repair` (deterministic JSON repair), `numpy` (Bradley-Terry fitting). **No framework-level dependencies.**
- Output structure is standard **JSON Schema draft 2020-12** — used directly as the LLM structured-output constraint and validated with `jsonschema`, no translation layer.
- Concurrency: `asyncio` with a per-profile `Semaphore(max_concurrency)`; full-jitter exponential backoff retries; consecutive fatal provider errors past `run.fatal_error_threshold` trip a circuit breaker (exit code 4).
- Default rubrics ship as package data at `labelkit/data/rubrics/default_text.toml` and `default_ui.toml` (full text in spec Appendix A).

## Architecture (spec §2.2)

Four layers; operator modules never depend on each other, only on the service layer and shared data structures:

- **Entry layer** — CLI: `labelkit run | validate | rubric`
- **Orchestration layer** — M10 orchestrator: batch splitting, stage composition per config switches, generation re-flow, lifecycle/memory disposal. Contains no business logic.
- **Operator layer** (uniform `Stage` protocol, independently switchable): M2 ingest, M3 dedup, M4 quality (QuRating), M5 annotate, M6 generate (off-path, default off), M7 verify, M11 emitter.
- **Service layer** (shared): M1 config, M8 schema-engine, M9 llm-client, M12 logging.

Data flows as `PipelineItem` envelopes (the only mutable type; lifetime = one batch) wrapping frozen `Record` dataclasses. Status values: `active`, `dropped_dup`, `dropped_lowq`, `dropped_verify`, `failed`. Core type contracts are in spec §4 — `Record`, `RecordRef`, `UITree`/`UINode`, `ImageRef`, `DedupInfo`, `QualityScore`, `Annotation`, `VerificationResult`, `StageError`.

**Stage contract** (spec §4.3): a stage only processes `status == "active"` items, never removes list elements (only changes status), and must never let a single-record failure escape to batch level — it goes into `item.errors` with `status = "failed"`. Exception: `generate` returns a new sub-batch instead.

Exception hierarchy → exit codes: `ConfigError` → 2, `InputError` → 3, provider fatal/circuit-break and unwritable output → 4, `--strict` with rejects → 1.

## Configuration (spec §2.5, §5)

Two TOML files, strictly separated; **no environment variables except API keys** (declared by *name* in config, value read from env):

- `config.toml` — tool-level, deployment-static: `[llm.<name>]` and `[embedding.<name>]` profiles (provider `openai_compatible` | `anthropic`, base_url, model, api_key_env, concurrency, retries, capability flags), log level/format.
- `project.toml` — per-run: input/output paths, modality (`text` | `ui`), `run.mode` (`process` | `generate_only`), batch size, seed, per-stage switches and parameters, rubric (inline or `default:text`/`default:ui`), task instructions + few-shot, output JSON Schema (inline or external file).

Precedence: CLI args > project.toml > config.toml. M1 merges and validates everything at startup (fail-fast, reports *all* errors at once) into an immutable `ResolvedConfig`.

Key stage-combination constraints M1 enforces (spec §2.3.1): `annotate` and `quality` can't both be disabled; `verify` requires `annotate`; `generate` requires text modality (and `quality` in process mode); `generate_only` requires `generate.enabled` and forbids `run.input`; `quality.threshold` and `quality.selection = "top_ratio"` are mutually exclusive.

## Non-negotiable constraints (spec §2.6)

- **No data persistence**: all intermediate state lives in process memory only; no temp files, no caches, no checkpoints, no cross-run state. Only explicit output channels touch disk: main output, rejects, `report.json`, and (opt-in) trace log. Reports contain counts/stats only, never data content.
- **LLM output is untrusted**: everything passes the M8 schema engine's four-layer guarantee (vendor structured output → deterministic repair → jsonschema validation → bounded LLM repair loop); irreparable records go to rejects, never to main output.
- **Record-level isolation**: one record's failure never affects others; the run continues.
- **Reproducibility**: `run.seed`-seeded PRNG for all sampling; temperature defaults to 0; output written to temp name + atomic rename.
- **Privacy**: data goes only to LLM endpoints declared in config; no telemetry; API keys never appear in logs or reports. stderr run logs never contain data content or prompts (trace channel redaction is tiered via `trace.content`).
- Scale target: ≤ 500k records per run (~2–4 GB RSS); image bytes are lazy-loaded per LLM request, never resident.

## CLI (spec §2.4)

```
labelkit run      --config config.toml --project project.toml
                  [--input PATH] [--output PATH] [--limit N] [--dry-run] [--strict] [--log-level LEVEL]
labelkit validate --config config.toml --project project.toml [--probe]
labelkit rubric   [--show default:text | default:ui]
```

Exit codes: 0 success (rejects allowed), 1 `--strict` violated or report write failure, 2 config error, 3 input error (process mode only), 4 fatal runtime error.

## Working with the spec

- Section map: §2 overall design, §3 per-module design (M1–M12, one subsection each with responsibilities/boundaries, I/O examples, algorithms, config keys), §4 shared data structures and internal API, §5 full config file field specs, §6 input/output format specs, §7 logging/observability and error classification codes (§7.6), §8 non-goals and roadmap, Appendix A default rubrics.
- Module numbering was reshuffled in v1.3 (annotate/generate split; generate is M6, old M6–M11 became M7–M12). Use the v1.4 numbering above.
- Every algorithm choice is backed by a cited paper or industrial project (§1.5) and several were explicitly decided with the stakeholder (§1.6) — don't substitute alternatives.
- The spec is the single source of truth for field names, defaults, and error codes; when implementing, copy them from the relevant table rather than inventing near-matches.
