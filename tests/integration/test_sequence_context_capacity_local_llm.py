"""完整会话和容量边界的真实本地 Qwen3.5-4B 门禁；不替换模型或 transport。"""
from __future__ import annotations

import hashlib
import json
import runpy
import shutil
import time
import tomllib
from collections import Counter
from pathlib import Path

import httpx
import pytest

from labelkit.cli.parser import CliOverrides
from labelkit.common.config import load
from labelkit.orchestration.application import execute_run

pytestmark = [pytest.mark.integration, pytest.mark.local_llm]

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "sequence-context-capacity"
ENDPOINT = "http://127.0.0.1:8082"
MODEL = "worldmock-qwen35-4b"
MODEL_SHA256 = "fdedd781c9ce676ab66b018ca247ff78e8a33c98098a822c1e2d5075e7718f66"


def copy_example(tmp_path):
    """复制输入、配置、独立检查器，排除以往运行工件。"""
    project = tmp_path / "sequence-context-capacity"
    shutil.copytree(EXAMPLE, project, ignore=shutil.ignore_patterns("out", "__pycache__"))
    (project / "out").mkdir()
    return project


def server_identity():
    """推理前确认真实单槽服务空闲，不修改配置或占用中的请求。"""
    with httpx.Client(timeout=20) as client:
        health = client.get(f"{ENDPOINT}/health")
        health.raise_for_status()
        assert health.json()["status"] == "ok"
        slots = client.get(f"{ENDPOINT}/slots").json()
        assert len(slots) == 1 and slots[0]["n_ctx"] == 32768
        assert slots[0]["is_processing"] is False, "The shared local slot is busy; run this gate after it finishes"
        props = client.get(f"{ENDPOINT}/props").json()
        models = client.get(f"{ENDPOINT}/v1/models").json()
    assert MODEL in {model["id"] for model in models["data"]}
    return {"model": MODEL, "model_sha256": MODEL_SHA256, "build": props["build_info"], "slots": 1, "n_ctx": 32768,
            "vision": props["modalities"]["vision"]}


def observe_requests(monkeypatch, texts=(), annotation_instruction=""):
    """透明观察生产序列化和真实 HTTP 返回，只保存哈希及结构证据。"""
    from labelkit.common.inference import llm_client

    original_body = llm_client._build_openai_body
    original_send = httpx.AsyncClient.send
    observations = {"requests": [], "responses": []}
    expected = Counter(texts)

    def stage_of(prompt):
        system = "\n".join(part.text or "" for message in prompt.messages if message.role == "system"
                           for part in message.parts if part.kind == "text")
        if system.startswith("你是数据分类员。阅读待分类数据，判断它属于以下类别中的哪一类。类别表："):
            return "classify"
        if system.startswith("你是标注质量审核员。给定任务指令、完整成员证据、动作序列与边界余量，独立判断该序列\n（episode）的标注是否合格。"):
            return "verify"
        prefix = annotation_instruction + "\n输出必须是符合以下 JSON Schema 的单个 JSON 对象，不输出任何其他内容：\n"
        if annotation_instruction and system.startswith(prefix):
            return "annotate"
        return "other"

    def body(profile, prompt, schema):
        result = original_body(profile, prompt, schema)
        text = "\n".join(part.text or "" for message in prompt.messages for part in message.parts if part.kind == "text")
        images = [part.image for message in prompt.messages for part in message.parts if part.kind == "image"]
        observations["requests"].append({
            "profile": profile.name, "stage": stage_of(prompt), "image_count": len(images),
            "image_hashes": [hashlib.sha256(image.path.read_bytes()).hexdigest() for image in images],
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(), "text_chars": len(text),
            "complete_input": bool(expected) and all(text.count(value) >= count for value, count in expected.items()),
            "thinking_disabled": result.get("chat_template_kwargs", {}).get("enable_thinking") is False,
        })
        return result

    async def send(client, request, **kwargs):
        response = await original_send(client, request, **kwargs)
        if request.url.path == "/v1/chat/completions":
            evidence = {"status": response.status_code,
                        "request_sha256": hashlib.sha256(request.content).hexdigest()}
            if response.status_code >= 400:
                payload = response.json()
                error = payload.get("error", payload)
                evidence["error"] = {key: error.get(key) for key in ("type", "code", "n_prompt_tokens", "n_ctx")}
            observations["responses"].append(evidence)
        return response

    monkeypatch.setattr(llm_client, "_build_openai_body", body)
    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    return observations


def run_project(project, project_name, output_name, config_name="config-local-4b.toml"):
    """经过正式生产装配面运行并读取实际 ResolvedPaths 工件。"""
    config = project / config_name
    source = project / project_name
    overrides = CliOverrides(output=str(project / "out" / output_name), console="plain")
    cfg = load(config, source, overrides)
    started = time.perf_counter()
    code = execute_run(config, source, overrides)
    report = json.loads(Path(cfg.paths.report).read_text(encoding="utf-8"))
    return cfg, report, {"exit_code": code, "wall_time_s": time.perf_counter() - started}


def save_evidence(project, name, evidence):
    """保留纯结构门禁证据，即使后续语义断言失败也可复查。"""
    path = project / "out" / f"{name}.evidence.json"
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")


def usage(report):
    """只接受实际成功请求的非零 token 用量。"""
    result = report["llm_usage"]["default"]
    assert result["calls"] > 0 and result["prompt_tokens"] > 0 and result["completion_tokens"] > 0
    return {key: result[key] for key in ("calls", "prompt_tokens", "completion_tokens")}


def test_real_local_text_batch_invariance(tmp_path, monkeypatch):
    identity = server_identity()
    project = copy_example(tmp_path)
    checker = runpy.run_path(str(project / "check_output.py"))
    texts = [row["text"] for row in checker["read_rows"](project / "data" / "events.jsonl")]
    source = (project / "project-text.toml").read_text(encoding="utf-8")
    observed = observe_requests(monkeypatch, texts, tomllib.loads(source)["annotate"]["instruction"])
    signatures = []
    for batch_size in (2, 3, 64):
        name = f"project-text-{batch_size}.toml"
        (project / name).write_text(source.replace("batch_size = 2", f"batch_size = {batch_size}"), encoding="utf-8")
        before = len(observed["requests"])
        cfg, report, run = run_project(project, name, f"text-{batch_size}.jsonl")
        requests = observed["requests"][before:]
        save_evidence(project, f"text-{batch_size}", {"server": identity, "run": run, "observed": observed})
        assert run["exit_code"] == 0
        rows, _ = checker["check_text"](cfg.paths.output)
        full = [request for request in requests if request["stage"] in {"annotate", "classify", "verify"}]
        assert {request["stage"] for request in full} == {"annotate", "classify", "verify"}
        assert all(request["complete_input"] for request in full)
        assert all(request["thinking_disabled"] for request in requests)
        assert len({member_id for member_id in rows[0]["_meta"]["stream"]["member_ids"]}) < 6
        usage(report)
        signatures.append(checker["semantic_signature"](rows))
    assert signatures[0] == signatures[1] == signatures[2]


def prepared_case(tmp_path, case):
    """测试准备只调用文件生成器，不代替生产推理。"""
    project = copy_example(tmp_path)
    runpy.run_path(str(project / "prepare.py"))["prepare"](project, case)
    return project, runpy.run_path(str(project / "check_output.py"))


def test_real_local_context_overflow_shape(tmp_path):
    identity = server_identity()
    text = " x" * 34000
    with httpx.Client(timeout=600) as client:
        tokenized = client.post(f"{ENDPOINT}/tokenize", json={"content": text, "add_special": False})
        tokenized.raise_for_status()
        raw_token_count = len(tokenized.json()["tokens"])
        assert raw_token_count >= identity["n_ctx"]
        body = {"model": MODEL, "messages": [{"role": "user", "content": text}],
                "max_tokens": 1, "temperature": 0, "stream": False,
                "chat_template_kwargs": {"enable_thinking": False}}
        response = client.post(f"{ENDPOINT}/v1/chat/completions", json=body)
    payload = response.json()
    error = payload.get("error", payload)
    evidence = {"server": identity, "status": response.status_code, "raw_token_count": raw_token_count,
                "error": {key: error.get(key) for key in ("type", "code", "n_prompt_tokens", "n_ctx")}}
    (tmp_path / "overflow-shape.evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    assert response.status_code == 400
    assert error["type"] == "exceed_context_size_error"
    assert error["n_prompt_tokens"] >= error["n_ctx"] == 32768


def test_real_local_ui_full_member_evidence(tmp_path, monkeypatch):
    identity = server_identity()
    project, checker = prepared_case(tmp_path, "ui")
    trees = [checker["read_rows"](project / "data" / "ui" / f"uitree_{index}.jsonl")[1]["text"]
             for index in range(1, 26)]
    source = tomllib.loads((project / "project-ui.toml").read_text(encoding="utf-8"))
    observed = observe_requests(monkeypatch, trees, source["annotate"]["instruction"])
    cfg, report, run = run_project(project, "project-ui.toml", "ui.jsonl")
    save_evidence(project, "ui", {"server": identity, "run": run, "observed": observed})
    assert run["exit_code"] == 0
    rows, _ = checker["check_ui"](cfg.paths.output)
    expected = [hashlib.sha256((project / "data" / "ui" / f"image_{index}.png").read_bytes()).hexdigest()
                for index in range(1, 26)]
    full = [request for request in observed["requests"] if request["stage"] in {"annotate", "verify"}]
    assert {request["stage"] for request in full} == {"annotate", "verify"}
    assert all(request["image_count"] == 25 for request in full)
    assert all(request["image_hashes"] == expected for request in full)
    assert all(request["complete_input"] for request in full)
    usage(report)


def test_real_local_interrupted_task_stitch(tmp_path, monkeypatch):
    identity = server_identity()
    project, checker = prepared_case(tmp_path, "stitch")
    observed = observe_requests(monkeypatch)
    cfg, report, run = run_project(project, "project-stitch.toml", "stitch.jsonl")
    save_evidence(project, "stitch", {"server": identity, "run": run, "observed": observed})
    assert run["exit_code"] == 0
    rows, _ = checker["check_structure"](cfg.paths.output, range(8))
    dispatch = [row for row in rows if row["task"] == "dispatch"]
    assert len(dispatch) == 1
    row = dispatch[0]
    assert {key: row[key] for key in ("request_id", "destination", "color", "quantity")} == {
        "request_id": "RIVER-42", "destination": "东仓", "color": "蓝色", "quantity": 7}
    assert row["_meta"]["stream"]["member_positions"] == [0, 1, 2, 5, 6, 7]
    assert len(row["_meta"]["stream"]["fragments"]) >= 2
    assert report["counts"]["stitched"] >= 1 and report["stream"]["stitch"]["judgments"] >= 1
    usage(report)


@pytest.mark.parametrize("case", ("static", "reactive"))
def test_real_local_capacity_recovery(tmp_path, monkeypatch, case):
    identity = server_identity()
    project, checker = prepared_case(tmp_path, case)
    observed = observe_requests(monkeypatch)
    cfg, report, run = run_project(project, f"project-{case}.toml", f"{case}.jsonl", f"config-{case}.toml")
    save_evidence(project, case, {"server": identity, "run": run, "observed": observed})
    assert run["exit_code"] == 0
    rows, _ = checker["check_capacity"](cfg.paths.output, case)
    capacity = report["stream"]["capacity"]
    assert capacity["sealed"] >= 1 and capacity["minimum_failures"] == 0
    errors = [response for response in observed["responses"] if response["status"] >= 400]
    if case == "reactive":
        assert all(row["_meta"]["stream"]["capacity"]["sealed"] for row in rows)
        assert errors and all(response["error"]["type"] == "exceed_context_size_error" for response in errors)
        assert capacity["splits"] >= 1 and capacity["recomputations"] >= 1
        assert len({response["request_sha256"] for response in errors}) == len(errors)
        assert all(row["_meta"]["stream"]["capacity"]["before"] is not None or
                   row["_meta"]["stream"]["capacity"]["after"] is not None for row in rows)
    else:
        assert [row["_meta"]["stream"]["capacity"]["sealed"] for row in rows] == [True] * (len(rows) - 1) + [False]
        assert capacity["sealed"] == capacity["splits"] == len(rows) - 1
        assert not errors and capacity["recomputations"] == 0
    assert report["counts"]["failed"] == 0
    usage(report)


def test_real_local_indivisible_capacity_failure(tmp_path, monkeypatch):
    identity = server_identity()
    project, checker = prepared_case(tmp_path, "minimum")
    observed = observe_requests(monkeypatch)
    cfg, report, run = run_project(project, "project-minimum.toml", "minimum.jsonl", "config-minimum.toml")
    save_evidence(project, "minimum", {"server": identity, "run": run, "observed": observed})
    assert run["exit_code"] == 0
    rows, _ = checker["check_structure"](cfg.paths.output, range(2))
    assert len(rows) == 1 and rows[0]["request_id"] == "CAPACITY"
    assert report["counts"]["failed"] >= 1 and report["stream"]["capacity"]["minimum_failures"] == 1
    failures = [response for response in observed["responses"] if response["status"] >= 400]
    assert len(failures) == 1 and failures[0]["error"]["type"] == "exceed_context_size_error"
    rejects = checker["read_rows"](cfg.paths.rejects)
    assert rejects and not any(row.get("request_id") == "CAPACITY" for row in rejects)
    usage(report)
