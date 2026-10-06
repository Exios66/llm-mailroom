"""Offline unit tests for the Modal smoke runner and its failure reports."""

from copy import deepcopy
import json
from types import SimpleNamespace

import httpx
import pytest

from llm import client
from pipeline.config import load_config
from scripts import smoke_modal_tiers as smoke


@pytest.fixture(autouse=True)
def clean_overrides(monkeypatch):
    monkeypatch.delenv("MAILROOM_GATEWAY_TIERS", raising=False)


@pytest.fixture
def taxonomy(monkeypatch):
    cfg = deepcopy(load_config())
    monkeypatch.setattr(client, "load_config", lambda: cfg)
    return cfg


@pytest.mark.parametrize("fault,message", [
    ("missing_alias", "has no model_list entry"),
    ("missing_tier", "not deployable"),
    ("wrong_alias", "!= served name"),
    ("missing_passthrough", "no '*' OpenRouter passthrough"),
])
def test_contract_rejects_inconsistent_deployments(taxonomy, monkeypatch, fault, message):
    aliases = {"mailroom-fast", "mailroom-extract", "mailroom-vision", "*"}
    tiers = {"fast", "extract", "vision"}
    if fault == "missing_alias":
        aliases.remove("mailroom-fast")
    elif fault == "missing_tier":
        tiers.remove("fast")
    elif fault == "wrong_alias":
        taxonomy["gateway"]["tiers"]["fast"]["alias"] = "wrong-alias"
        aliases.add("wrong-alias")
    else:
        aliases.remove("*")
        monkeypatch.setenv("MAILROOM_GATEWAY_TIERS", "sorter=api")
    monkeypatch.setattr(smoke, "litellm_aliases", lambda: aliases)
    monkeypatch.setattr(smoke, "modal_tiers", lambda: tiers)
    failures, routing = smoke.check_contract(taxonomy)
    assert len(failures) == 1
    assert message in failures[0]
    assert "sorter" in routing


def test_invalid_override_is_returned_as_contract_failure(taxonomy, monkeypatch):
    monkeypatch.setenv("MAILROOM_GATEWAY_TIERS", "sorter=typo")
    failures, routing = smoke.check_contract(taxonomy)
    assert routing == {}
    assert len(failures) == 1
    assert "unknown gateway tier 'typo'" in failures[0]


def test_routing_excludes_procedural_agents_and_honors_api_override(taxonomy, monkeypatch):
    taxonomy["agents"]["test_clerk"] = {"procedural": True}
    monkeypatch.setenv("MAILROOM_GATEWAY_TIERS", "sorter=api")
    routing = smoke.expected_routing(taxonomy)
    assert "test_clerk" not in routing
    assert routing["sorter"] == {
        "tier": "api", "wire_model": taxonomy["agents"]["sorter"]["model"], "gpu": False,
    }


@pytest.mark.parametrize("parent_key", ["parent_observation_id", "parentObservationId"])
def test_named_agent_is_checked_more_strictly_than_parent_node(parent_key):
    routing = {
        "sorter": {"tier": "fast", "wire_model": "mailroom-fast"},
        "sorter_reviewer": {"tier": "extract", "wire_model": "mailroom-extract"},
    }
    observations = [
        {"id": "node", "type": "SPAN", "name": "classify-document"},
        {"id": "nested", "type": "SPAN", "name": "invoke", parent_key: "node"},
        {"id": "gen", "type": "generation", "name": "sorter", "model": "mailroom-extract", parent_key: "nested"},
    ]
    rows, failures = smoke.verify_generations(observations, routing, None)
    assert len(failures) == 1
    assert rows == [{
        "node": "classify-document", "generation": "sorter", "model": "mailroom-extract",
        "expected": ["mailroom-fast"], "tiers": ["fast"], "ok": False,
    }]
    observations[-1]["name"] = "ChatOpenAI"
    rows, failures = smoke.verify_generations(observations, routing, None)
    assert failures == []
    assert rows[0]["ok"] is True
    assert rows[0]["expected"] == ["mailroom-extract", "mailroom-fast"]


@pytest.mark.parametrize("parents", [
    {},
    {"a": {"name": "wrapper", "parentObservationId": "b"},
     "b": {"name": "wrapper", "parentObservationId": "a"}},
])
def test_missing_and_cyclic_parents_do_not_hang(parents):
    assert smoke.node_of({"parentObservationId": "a"}, parents) is None


@pytest.mark.parametrize("model,ok", [
    (None, False), ("", False), ("vendor/model", True),
    ("vendor/model-2026-01", True), ("vendor/modelish", False),
])
def test_generation_requires_model_and_respects_variant_boundary(model, ok):
    routing = {"sorter": {"tier": "api", "wire_model": "vendor/model"}}
    rows, failures = smoke.verify_generations([
        {"type": "GENERATION", "name": "sorter", "model": model},
        {"type": "GENERATION", "name": "pipeline-result", "model": None},
        {"type": "SPAN", "name": "sorter", "model": "unrelated"},
    ], routing, None)
    assert len(rows) == 1
    assert rows[0]["node"] == "(unattributed)"
    assert rows[0]["ok"] is ok
    assert len(failures) == (0 if ok else 1)


def test_extraction_without_known_specialist_cannot_claim_success():
    observations = [
        {"id": "node", "name": "extract-fields", "type": "SPAN"},
        {"type": "GENERATION", "name": "ChatOpenAI", "model": "mailroom-extract", "parentObservationId": "node"},
    ]
    rows, failures = smoke.verify_generations(observations, {}, None)
    assert rows[0]["expected"] == []
    assert rows[0]["ok"] is False
    assert len(failures) == 1


def test_document_selection_ignores_blanks_and_breaks_ties_by_filename():
    rows = [
        {"class": "contract", "filename": "z", "doc_text": "x" * 12},
        {"class": "contract", "filename": "a", "doc_text": "x" * 8},
        {"class": "contract", "filename": "blank", "doc_text": " " * 10},
        {"class": "insurance_claim", "filename": "missing", "doc_text": None},
        {"class": "unrequested", "filename": "other", "doc_text": "x" * 10},
    ]
    original = deepcopy(rows)
    for candidates in (rows, list(reversed(rows))):
        assert smoke.pick_documents(candidates, ["contract", "insurance_claim"], 10) == {"contract": rows[1]}
    assert rows == original


def test_document_writer_sanitizes_basename_and_preserves_unicode(tmp_path):
    row = {"filename": "../../unsafe name?.pdf", "doc_text": "Résumé — § 12"}
    path = smoke.write_document(tmp_path, "contract", row, "stamp")
    assert path.parent == tmp_path
    assert path.name == "smoke_stamp_contract_unsafe_name_.txt"
    assert path.read_text(encoding="utf-8") == row["doc_text"]


def test_warmup_deduplicates_gpu_aliases_and_skips_api(mocker):
    cfg = {"gateway": {"tiers": {
        "fast": {"alias": "mailroom-fast"}, "duplicate": {"alias": "mailroom-fast"},
        "vision": {"alias": "mailroom-vision"}, "api": {},
    }}}
    sdk = mocker.patch.object(smoke, "_gateway_client")
    create = sdk.return_value.chat.completions.create
    create.side_effect = lambda **kwargs: SimpleNamespace(model=kwargs["model"])
    results = smoke.warm_tiers(cfg, timeout=5)
    assert set(results) == {"mailroom-fast", "mailroom-vision"}
    assert all(status.startswith("ok") for status in results.values())
    assert create.call_count == 2
    assert {call.kwargs["model"] for call in create.call_args_list} == set(results)
    for call in create.call_args_list:
        assert call.kwargs["max_tokens"] == 1
        assert call.kwargs["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}


def test_warmup_reports_failure_after_deadline_without_real_sleep(mocker):
    sdk = mocker.patch.object(smoke, "_gateway_client")
    sdk.return_value.chat.completions.create.side_effect = RuntimeError("still cold")
    mocker.patch.object(smoke.time, "perf_counter", side_effect=[0, 0, 16])
    sleep = mocker.patch.object(smoke.time, "sleep")
    results = smoke.warm_tiers({"gateway": {"tiers": {"fast": {"alias": "mailroom-fast"}}}}, timeout=10)
    assert results == {"mailroom-fast": "FAILED after 10s — RuntimeError: still cold"}
    sleep.assert_called_once_with(15)
    sdk.return_value.chat.completions.create.assert_called_once()


@pytest.mark.parametrize("terminal_stage", ["archived", "review", "failed", "aborted", None])
def test_api_runner_matches_uploaded_name_and_records_timeouts(tmp_path, mocker, terminal_stage):
    path = tmp_path / "input.txt"
    path.write_text("Synthetic document")
    factory = mocker.patch("httpx.Client")
    http = factory.return_value.__enter__.return_value
    http.post.return_value.json.return_value = {"file": "server-renamed.txt"}
    http.get.return_value.json.return_value = {"documents": [
        {"original_filename": "unrelated.txt", "stage": "archived"},
        {"original_filename": "server-renamed.txt", "stage": terminal_stage or "processing",
         "doc_type": "contract", "doc_id": "doc-1"},
    ]}
    mocker.patch.object(smoke.time, "time", side_effect=[0, 0, 11])
    sleep = mocker.patch.object(smoke.time, "sleep")
    results = smoke.run_via_api("http://api.test/", "test-token", {"contract": path}, "matter-1", 10)
    factory.assert_called_once_with(base_url="http://api.test", headers={"Authorization": "Bearer test-token"}, timeout=60.0)
    http.post.return_value.raise_for_status.assert_called_once_with()
    assert http.post.call_args.kwargs["data"] == {"matter_id": "matter-1"}
    http.get.assert_called_once_with("/matters/matter-1")
    if terminal_stage:
        assert results["contract"] == {
            "stage": terminal_stage, "doc_type": "contract", "doc_id": "doc-1", "filename": "server-renamed.txt",
        }
        sleep.assert_not_called()
    else:
        assert results["contract"] == {
            "stage": "timeout", "doc_type": None, "doc_id": None, "filename": "input.txt",
        }
        sleep.assert_called_once_with(10)


def test_api_upload_failure_stops_polling(tmp_path, mocker):
    path = tmp_path / "input.txt"
    path.write_text("Synthetic document")
    http = mocker.patch("httpx.Client").return_value.__enter__.return_value
    response = httpx.Response(401, request=httpx.Request("POST", "http://api.test/upload"))
    http.post.return_value = response
    with pytest.raises(httpx.HTTPStatusError):
        smoke.run_via_api("http://api.test", "", {"contract": path}, "matter", 10)
    http.get.assert_not_called()


@pytest.mark.parametrize("failure", [None, "missing-traces", "fetch-error", "missing-generations", "wrong-model"])
def test_verify_session_exit_status_and_reports(tmp_path, monkeypatch, mocker, failure):
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(tmp_path))
    routing = {"sorter": {"tier": "fast", "wire_model": "mailroom-fast", "gpu": True}}
    mocker.patch.object(smoke, "check_contract", return_value=([], routing))
    fetch = mocker.patch.object(smoke, "fetch_session_observations")
    if failure == "fetch-error":
        fetch.side_effect = RuntimeError("offline")
    elif failure == "missing-traces":
        fetch.return_value = {}
    elif failure == "missing-generations":
        fetch.return_value = {"trace": [{"type": "GENERATION", "name": "pipeline-result"}]}
    else:
        fetch.return_value = {"trace": [{"type": "GENERATION", "name": "sorter",
                                        "model": "wrong" if failure else "mailroom-fast"}]}
    assert smoke.main(["--verify-session", "session-1"]) == (1 if failure else 0)
    fetch.assert_called_once_with("session-1", expect_traces=1)
    reports = list(tmp_path.glob("smoke_runs/*/report.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text())
    assert report["passed"] is (failure is None)
    assert report["session_id"] == "session-1"
    assert bool(report["failures"]) is bool(failure)
    markdown = reports[0].with_suffix(".md").read_text()
    assert ("**FAIL**" if failure else "**PASS**") in markdown
    for problem in report["failures"]:
        assert problem in markdown


def test_default_cli_checks_contract_without_running_external_operations(mocker):
    mocker.patch.object(smoke, "check_contract", return_value=(["invalid route"], {}))
    warm = mocker.patch.object(smoke, "warm_tiers")
    run = mocker.patch.object(smoke, "run_via_api")
    fetch = mocker.patch.object(smoke, "fetch_session_observations")
    report = mocker.patch.object(smoke, "write_report")
    assert smoke.main([]) == 1
    for operation in (warm, run, fetch, report):
        operation.assert_not_called()


@pytest.mark.parametrize("stage,problem", [
    ("archived", None), ("review", None),
    ("failed", "pipeline failed"), ("timeout", "did not finish"),
])
def test_run_cli_reports_document_outcomes(tmp_path, monkeypatch, mocker, stage, problem):
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(tmp_path))
    mocker.patch.object(smoke, "check_contract", return_value=([], {}))
    load = mocker.patch.object(smoke, "load_rows", return_value=[{
        "class": "contract", "filename": "sample.txt", "doc_text": "Synthetic contract",
    }])
    run = mocker.patch.object(smoke, "run_via_api", return_value={"contract": {
        "stage": stage, "doc_type": "contract", "doc_id": "doc-1", "filename": "sample.txt",
    }})
    fetch = mocker.patch.object(smoke, "fetch_session_observations")
    assert smoke.main(["--run", "--source", "snapshot", "--classes", "contract", "--skip-langfuse"]) == (1 if problem else 0)
    load.assert_called_once_with("snapshot")
    run.assert_called_once()
    files = run.call_args.args[2]
    assert set(files) == {"contract"}
    assert files["contract"].read_text() == "Synthetic contract"
    fetch.assert_not_called()
    report = json.loads(next(tmp_path.glob("smoke_runs/*/report.json")).read_text())
    assert report["documents"]["contract"]["stage"] == stage
    assert report["passed"] is (problem is None)
    assert report["session_id"] == run.call_args.args[3]
    if problem:
        assert len(report["failures"]) == 1
        assert problem in report["failures"][0]
    else:
        assert report["failures"] == []


def test_missing_document_class_is_a_failed_smoke_run(tmp_path, monkeypatch, mocker):
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(tmp_path))
    mocker.patch.object(smoke, "check_contract", return_value=([], {}))
    mocker.patch.object(smoke, "load_rows", return_value=[])
    run = mocker.patch.object(smoke, "run_via_api", return_value={})
    assert smoke.main(["--run", "--source", "snapshot", "--classes", "contract", "--skip-langfuse"]) == 1
    assert run.call_args.args[2] == {}
    report = json.loads(next(tmp_path.glob("smoke_runs/*/report.json")).read_text())
    assert report["failures"] == ["no snapshot document for class 'contract'"]


def test_polling_recovers_from_consecutive_http_and_json_failures(tmp_path, monkeypatch):
    path = tmp_path / "contract.txt"
    path.write_text("Synthetic contract")
    now = [0]
    polls = []

    def handle(request):
        if request.method == "POST":
            return httpx.Response(200, json={"file": path.name})
        polls.append(now[0])
        if len(polls) == 1:
            return httpx.Response(503, text="starting")
        if len(polls) == 2:
            return httpx.Response(200, text="malformed JSON")
        return httpx.Response(200, json={"documents": [{
            "original_filename": path.name, "stage": "archived",
            "doc_type": "contract", "doc_id": "doc-1",
        }]})

    def sleep(seconds):
        now[0] += seconds

    http = httpx.Client(transport=httpx.MockTransport(handle), base_url="http://api.test")
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: http)
    monkeypatch.setattr(smoke.time, "time", lambda: now[0])
    monkeypatch.setattr(smoke.time, "sleep", sleep)
    assert smoke.run_via_api("http://api.test", "", {"contract": path}, "matter", 30) == {
        "contract": {"stage": "archived", "doc_type": "contract", "doc_id": "doc-1", "filename": path.name},
    }
    assert polls == [0, 10, 20]
    assert now[0] == 20
    assert http.is_closed


def test_zero_polling_budget_returns_timeout_without_fetching(tmp_path, mocker):
    path = tmp_path / "contract.txt"
    path.write_text("Synthetic contract")
    http = mocker.patch("httpx.Client").return_value.__enter__.return_value
    http.post.return_value.json.return_value = {"file": path.name}
    mocker.patch.object(smoke.time, "time", return_value=10)
    sleep = mocker.patch.object(smoke.time, "sleep")
    assert smoke.run_via_api("http://api.test", "", {"contract": path}, "matter", 0) == {
        "contract": {"stage": "timeout", "doc_type": None, "doc_id": None, "filename": path.name},
    }
    http.get.assert_not_called()
    sleep.assert_not_called()
