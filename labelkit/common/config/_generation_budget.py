"""v1.18 序列提示词的启动期上下文预算证明。"""
from __future__ import annotations

import json
from typing import Mapping

from jsonschema import Draft202012Validator

from labelkit.common.runtime import budget
from labelkit.common.runtime.llm_client import Message, Part, PromptBundle


def check_generation_content_limits(state, config) -> None:
    """检查生成提示文本与运行期内容 Schema 的固定上限。

    @param state generation 聚合解析状态
    @param config 完整 sequence 配置
    @return None
    """
    _check_prompt_texts(state, config)
    _check_schema_examples(state, config)


def _check_prompt_texts(state, config) -> None:
    """检查每项生成提示文本的 UTF-8 byte 上限。"""
    texts = [view.description for view in state.context.class_views.values()]
    texts.extend(view.sequence_generation.instruction
                 for view in state.context.class_views.values()
                 if view.sequence_generation is not None)
    texts.extend(view.description for view in state.context.frame_classes.values())
    texts.extend(view.gen_instruction or "" for view in state.context.frame_classes.values())
    texts.extend(item.description for item in config.patterns)
    texts.extend(role.state_instruction for item in config.patterns for role in item.roles)
    texts.extend(item.instruction for item in config.instruction_only)
    if config.noise is not None:
        texts.append(config.noise.instruction)
        texts.extend(config.noise.topics)
    for value in texts:
        if len(value.encode("utf-8")) > config.limits.prompt_text_bytes:
            state.context.collector.error(
                f"{state.context.project_root / 'project.toml'}:[generate]: "
                "generation prompt text exceeds 32768 UTF-8 bytes"
            )


def _check_schema_examples(state, config) -> None:
    """要求运行期内容 Schema 提供至少一个可实现的小型根 example。"""
    limit = config.limits.prompt_value_bytes
    required_frames = _required_frame_names(state, config)
    for name, view in state.context.frame_classes.items():
        if name in required_frames:
            _schema_example(
                state, view.gen_schema, f"[frame.class.{name}.generate]",
                min(limit, config.limits.rendered_payload_bytes),
            )
    for index, source in enumerate(config.instruction_only, 1):
        if _instruction_state_is_explicit(state, index):
            _schema_example(
                state, source.state_schema,
                f"[[generate.instruction_only]][{index}].state", limit,
            )
    for name, view in state.context.class_views.items():
        generation = view.sequence_generation
        if generation is not None and generation.initial_state_source == "llm":
            _schema_example(
                state, generation.state_schema, f"[class.{name}.generate].state", limit,
            )
    for source in config.counterfactual_sets:
        for variant in source.variants:
            _schema_example(
                state, variant.outcome_schema,
                f"[generate.counterfactual_sets.{source.name}.{variant.name}.outcome]",
                limit,
            )


def _required_frame_names(state, config) -> set[str]:
    """返回当前模式确实可能调用 FrameRenderer 的帧类名。"""
    noise = None if config.noise is None else config.noise.frame_class
    if config.mode == "declared":
        names = {role.frame_class for pattern in config.patterns for role in pattern.roles}
    else:
        names = {
            name for name, view in state.context.frame_classes.items()
            if name != noise and _frame_is_generatable(view)
        }
    if noise is not None:
        names.add(noise)
    return names


def _frame_is_generatable(view) -> bool:
    """返回帧类是否具备 instruction-only 生成所需的完整契约。"""
    return bool(view.description.strip() and view.gen_instruction and view.gen_schema is not None)


def _schema_example(state, schema, location: str, byte_limit: int):
    """验证并确定性选择一条 root examples object。"""
    plain_schema = _thaw_json(schema)
    examples = plain_schema.get("examples") if isinstance(plain_schema, Mapping) else None
    if not isinstance(examples, list) or not examples:
        _schema_error(state, location, "generation schema requires a non-empty root examples array")
        return None
    validator = Draft202012Validator(plain_schema)
    valid = [item for item in examples
             if isinstance(item, dict) and not list(validator.iter_errors(item))]
    if not valid:
        _schema_error(state, location, "generation schema root examples contain no valid object")
        return None
    selected = min(valid, key=_canonical_example_key)
    if _canonical_example_key(selected)[0] > byte_limit:
        _schema_error(
            state, location, "smallest valid root example exceeds the prompt value byte limit"
        )
    return selected


def _instruction_state_is_explicit(state, index: int) -> bool:
    """判断一条 instruction-only 是否显式声明 state Schema。

    @param state generation 聚合解析状态。
    @param index instruction-only 一基序号。
    @return 当前行显式声明 state_schema_path 时为 true。
    """
    rows = state.generate.get("instruction_only")
    if not isinstance(rows, list) or not 1 <= index <= len(rows):
        return False
    row = rows[index - 1]
    return isinstance(row, Mapping) and "state_schema_path" in row


def _schema_error(state, location: str, message: str) -> None:
    """把根 example 违规写入 M1 聚合错误。"""
    state.context.collector.error(
        f"{state.context.project_root / 'project.toml'}:{location}: {message}"
    )


def _canonical_example_key(value) -> tuple[int, bytes]:
    """返回 root example 的确定性最小化键。"""
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return len(encoded), encoded


def check_generation_context_budget(state, config, cases) -> None:
    """验证所有允许的首轮与修复轮提示词都能装入声明窗口。

    case 内的 PromptBundle 保留配置态完整脚手架；每个运行期动态值另以固定
    canonical UTF-8 byte 上界折算，从而不会把空对象 witness 当作真实上界。

    @param state generation 聚合解析状态
    @param config 完整 sequence 配置
    @param cases 六个 prompt family 的配置态 case
    @return None
    """
    costs = _initial_budget_costs(state.context.llm_profiles, cases)
    _merge_repair_budget_costs(state, config, cases, costs)
    for name, cost in costs.items():
        profile = state.context.llm_profiles.get(name)
        if profile is not None and cost > budget.input_budget(profile):
            state.context.collector.error(
                f"{state.context.project_root / 'project.toml'}:"
                f"[llm.{name}].context_window: complete sequence prompt and Schema "
                "do not fit the input budget"
            )


def _initial_budget_costs(profiles: Mapping[str, object], cases) -> dict[str, int]:
    """按首轮 profile 合并完整静态脚手架与动态值上界。"""
    costs: dict[str, int] = {}
    for case in cases:
        profile = profiles.get(case.profile)
        if profile is None:
            continue
        cost = _case_cost(case, profile)
        costs[case.profile] = max(costs.get(case.profile, 0), cost)
    return costs


def _merge_repair_budget_costs(state, config, cases, costs) -> None:
    """把 generic 与 EventPlan replay 修复轮的完整上界并入 profile。"""
    if state.context.max_repair_attempts == 0:
        return
    for case in cases:
        name = state.context.repair_profile or case.profile
        profile = state.context.llm_profiles.get(name)
        if profile is None:
            continue
        if case.post_validated:
            cost = _post_repair_cost(case, profile, config.limits.repair_context_bytes)
        else:
            cost = _generic_repair_cost(
                case.schema, profile, config.limits.repair_context_bytes
            )
        costs[name] = max(costs.get(name, 0), cost)


def _case_cost(case, profile) -> int:
    """计算一个首轮 case 的保守 token 上界。"""
    schema = _thaw_json(case.schema) if profile.supports_structured_output else None
    base = budget.est_prompt(case.prompt, profile, schema, 0)
    return base + sum(_byte_token_bound(item) for item in case.dynamic_byte_limits)


def _post_repair_cost(case, profile, repair_context_bytes: int) -> int:
    """计算重放原 prompt 并追加两个修复消息的 token 上界。"""
    return (
        _case_cost(case, profile)
        + _byte_token_bound(repair_context_bytes)
        + 2 * budget.MSG_OVERHEAD_TOKENS
    )


def _generic_repair_cost(schema_value, profile, repair_context_bytes: int) -> int:
    """计算只含一个 user 修复消息的 token 上界。"""
    prompt = PromptBundle(messages=(
        Message(role="user", parts=(Part(kind="text", text=""),)),
    ))
    schema = _thaw_json(schema_value) if profile.supports_structured_output else None
    return (
        budget.est_prompt(prompt, profile, schema, 0)
        + _byte_token_bound(repair_context_bytes)
    )


def _byte_token_bound(byte_count: int) -> int:
    """把 canonical UTF-8 byte 上界转换为当前估算器的保守 token 上界。"""
    return (byte_count + 2) // 3


def _thaw_json(value: object) -> object:
    """把冻结 JSON 树递归复制成标准容器。"""
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw_json(item) for item in value]
    return value
