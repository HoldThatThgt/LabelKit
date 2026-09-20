# Bob report

Status: COMPLETE

## Verdict

The final clean snapshot `8838b61821839f42de0b81c05892b13d8c17d698` passed the declared six-file
baseline with **1048 passed in 6.43s**. All **23/23** independent semantic mutants were then killed by causal
assertion failures. Final counts are **23 killed, 0 survived, 0 invalid, 0 inconclusive**.

The review found two real test gaps in earlier snapshots. The first allowed a retry attempt to receive a larger total
deadline; the second allowed a passing stream judge's opinion to enter repair instructions. Each gap was reported before
any test change, hardened in a new clean snapshot, and followed by a complete M01-M23 rerun. Production code did not
change between the three snapshots.

| Final classification | Count |
|---|---:|
| Killed | 23 |
| Survived | 0 |
| Invalid | 0 |
| Inconclusive | 0 |

## Scope and source of truth

The normative clauses reviewed were:

- `spec/309-m9-llm-client.md:113-116` and `docs/CONTRACTS.md:3722-3727`: per-attempt total deadline after origin
  admission, retryable timeout classification, external cancellation, and PoolTimeout classification.
- `spec/308-m8-schema-engine.md:34,74-75,96-97` and `docs/CONTRACTS.md:3215-3233,8222-8242`: count-only
  `maxItems`, ordinary retained repair prompts, complete-evidence prompts, and distinct overflow behavior.
- `spec/305-m5-annotate.md:80-82,118`: process annotation retention, cumulative repair instruction, and reachable
  UI repair-profile vision validation.
- `spec/307-m7-verify.md:50-55` and `docs/CONTRACTS.md:3078`: cumulative fail-voter history in round/panel order,
  pass-voter exclusion from repair, snapshot isolation, member surgery, seam refresh, and independent re-review.

Only these production files were mutated:

- `labelkit/common/config/_constraints.py`
- `labelkit/common/inference/llm_client.py`
- `labelkit/common/inference/schema_engine.py`
- `labelkit/operators/annotate.py`
- `labelkit/operators/verify.py`
- `labelkit/operators/stream_verify.py`

Every baseline and mutant used the same test selection:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 \
PYTHONPATH=<isolated-worktree> \
/Users/atishoo/Project/LabelKit/.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/common/config/test_config.py \
  tests/common/contracts/test_generation_contracts.py \
  tests/common/inference/test_llm_client.py \
  tests/common/inference/test_schema_engine.py \
  tests/operators/test_annotate.py \
  tests/operators/test_verify.py
```

The imported `labelkit.__file__` was checked before each baseline and resolved inside the corresponding mutation
worktree. No `.env` was present. This review made no network request, ran no integration marker, and created no mock
server, transport, or recorded provider response.

## Review history

| Snapshot | Executed evidence | Result | Action |
|---|---|---|---|
| `e668c173ec994c26896a17bd97cb00b3d5e90a48` | Baseline `1047 passed in 7.38s`; M01-M12 only | 11 killed; M05 survived with `1047 passed in 6.88s` | Reported that retries could receive `timeout_s * (attempt + 1)` without detection. |
| `46f59ab20663884a0858dd79491c17f778726cdc` | Baseline `1047 passed in 7.33s`; complete M01-M23 | 22 killed; M23 survived with `1047 passed in 6.35s` | Reported that stream pass-voter critiques could enter repair undetected. |
| `8838b61821839f42de0b81c05892b13d8c17d698` | Baseline `1048 passed in 6.43s`; complete M01-M23 | 23 killed; zero survived, invalid, or inconclusive | Final gate passed. |

The M05 oracle now blocks each real HTTPX request in a request hook for `1.5 * timeout_s`, requires cancellation near
the configured deadline on both attempts, and never sends a network request
(`tests/common/inference/test_llm_client.py:1987`). The M23 oracle runs the real stream driver with a
`fail / pass / fail` panel, requires repair to contain only fail-voter critiques in judge order, keeps every opinion in
the review result, and proves re-review receives no history (`tests/operators/test_verify.py:1367-1402`).

## Final mutation matrix

| ID | Semantic mutation | Causal oracle in final snapshot | Exact final result |
|---|---|---|---|
| M01 | Disable the total-attempt deadline with `asyncio.timeout(None)` | Total deadline, queue exclusion, and fresh retry-budget tests | 3 failed, 1045 passed |
| M02 | Remove `TimeoutError` from the retryable transport branch | Same three total-deadline tests observe raw `TimeoutError` | 3 failed, 1045 passed |
| M03 | Convert external `CancelledError` into an attempt failure | `test_external_cancellation_preserves_control_flow_and_origin_permit` | 1 failed, 1047 passed |
| M04 | Stop classifying admitted `PoolTimeout` as an internal capacity error | `test_pool_timeout_remains_internal_after_explicit_admission` | 1 failed, 1047 passed |
| M05 | Scale the second attempt's total deadline by attempt number | `test_deadline_retries_receive_a_fresh_attempt_budget` reaches its no-cancellation failure | 1 failed, 1047 passed |
| M06 | Remove the count-only `maxItems` renderer | Nested and root `maxItems` wording/payload assertions | 3 failed, 1045 passed |
| M07 | Report actual array length minus one | Nested and root received-count assertions | 3 failed, 1045 passed |
| M08 | Drop retained prompt wiring from `complete_validated` | Ordinary retained-evidence tests with `finalized=False` | 2 failed, 1046 passed |
| M09 | Drop retained prompt wiring from `complete_finalized` | Ordinary retained-evidence tests with `finalized=True` | 2 failed, 1046 passed |
| M10 | Propagate overflow whenever any original prompt is retained | Four ordinary retained-prompt overflow cases expect `SchemaViolation` and reactive feed rules | 4 failed, 1044 passed |
| M11 | Swallow complete-evidence overflow as an ordinary schema failure | Validated and finalized complete-evidence overflow tests | 2 failed, 1046 passed |
| M12 | Disable process annotation's retained repair prompt | Process class-schema and existing call-shape routing tests | 2 failed, 1046 passed |
| M13 | Retain the repair prompt in `generate_only` record annotation | Generate-only negative routing assertion | 1 failed, 1047 passed |
| M14 | Remove process annotation from reachable UI repair vision users | Non-stream process repair-profile rejection case | 1 failed, 1047 passed |
| M15 | Require repair-profile vision even when repair attempts are zero | Zero-attempt and disabled stream-schema cases | 2 failed, 1046 passed |
| M16 | Regress the cumulative repair instruction to `请修正后重新输出` | Text, UI, and sequence repair prompt assertions | 3 failed, 1045 passed |
| M17 | Give the helper repair only the current round's fail critiques | Two-round cumulative-history helper tests | 2 failed, 1046 passed |
| M18 | Expose the helper's mutable history list to the repair callback | Snapshot-isolation and two-round helper tests | 2 failed, 1046 passed |
| M19 | Replace classic fail history on every round | Classic single/multi-judge cumulative-history test | 2 failed, 1046 passed |
| M20 | Replace stream fail history on every round | Stream cumulative-history and member-surgery tests | 2 failed, 1046 passed |
| M21 | Replace history when seam evidence changes instead of appending refresh | Seam dependency history test | 1 failed, 1047 passed |
| M22 | Add classic pass-voter opinions to repair history | Classic mixed-panel fail-only assertion | 1 failed, 1047 passed |
| M23 | Add stream pass-voter opinions to repair history while preserving fail-only defects | `test_stream_panel_repair_uses_only_fail_voter_critiques`; failure exposes `pass_b/clarity: PASS_B_HISTORY` | 1 failed, 1047 passed |

All failures arose after normal test collection and execution. A scan of every final mutant log found no collection
error, `SyntaxError`, internal pytest error, or import failure, so none of the 23 mutants was classified invalid.

## Restoration and artifacts

Each final ledger entry records `restored_clean=true`. After M23, both `git diff --exit-code` and
`git status --porcelain=v1 --untracked-files=all` were empty at HEAD
`8838b61821839f42de0b81c05892b13d8c17d698`. The mutation worktree was removed, its temporary directory was removed,
the caller remained clean at the same HEAD, and no Bob review worktree remained registered.

Artifacts:

- First-round raw logs: `out/verification-common-fixes-20260920/bob/`
- Second-round raw logs: `out/verification-common-fixes-20260920/bob/rereview/`
- Final baseline, raw logs, and machine-readable ledger:
  `out/verification-common-fixes-20260920/bob/final/`
- Reproducible sequential mutation driver: `out/verification-common-fixes-20260920/bob/run_mutants.py`

The reviewer changed no production source, test, specification, or caller checkout. Only review artifacts under `out/`
were produced.

## Residual boundary

This is a targeted specification-driven mutation review, not an exhaustive mutation score for all six production files.
It establishes that the selected 23 behavior-breaking changes are rejected by the final 1048-test gate. Real endpoint
transport, provider parsing, semantic quality, and repeated-run stability were outside this Bob run and are separate
evidence layers. No review blocker or parent decision remains for the reviewed clauses.
