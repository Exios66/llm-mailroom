"""Network-free pins for the hosted Spaces probe."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "src" / "scripts" / "probe_hosted_spaces.py"


def _load():
    spec = importlib.util.spec_from_file_location("probe_hosted_spaces", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_offline_pins_live_pair():
    mod = _load()
    pins = mod.offline_pins()
    assert pins["observatory_hub"] == "Lucius-Morningstar/mailroom-observatory"
    assert pins["producer_hub"] == "Lucius-Morningstar/mailroom-producer"
    assert pins["observatory_host"].startswith("https://lucius-morningstar-")
    assert pins["producer_host"].startswith("https://lucius-morningstar-")
    assert mod.main(["--offline"]) == 0


def test_probe_observatory_up_producer_missing():
    mod = _load()

    def fake_fetch(url: str, **_kwargs):
        if url.endswith("/Lucius-Morningstar/mailroom-observatory"):
            return 200, json.dumps({"runtime": {"stage": "RUNNING"}})
        if url.endswith("/api/health"):
            return 200, json.dumps(
                {
                    "ok": True,
                    "langfuse": True,
                    "pipeline_configured": False,
                    "cache": {"cached_trace_count": 74},
                }
            )
        if url.endswith("/api/pipeline"):
            return 200, json.dumps(
                {"configured": False, "ok": None, "watcher": "unconfigured"}
            )
        if url.endswith("/Lucius-Morningstar/mailroom-producer"):
            return 404, '{"error":"not found"}'
        if url.endswith("/health"):
            return 404, "not found"
        return 0, "unexpected"

    result = mod.probe(fetch_fn=fake_fetch)
    assert result["observatory"]["ok"] is True
    assert result["observatory"]["cached_trace_count"] == 74
    assert result["producer"]["ok"] is False
    assert result["paired"] is False


def test_probe_paired_when_producer_health_ok():
    mod = _load()

    def fake_fetch(url: str, **_kwargs):
        if url.endswith("/Lucius-Morningstar/mailroom-observatory"):
            return 200, json.dumps({"runtime": {"stage": "RUNNING"}})
        if url.endswith("/api/health"):
            return 200, json.dumps(
                {
                    "ok": True,
                    "langfuse": True,
                    "pipeline_configured": True,
                    "cache": {"cached_trace_count": 3},
                }
            )
        if url.endswith("/api/pipeline"):
            return 200, json.dumps(
                {"configured": True, "ok": True, "watcher": "ok"}
            )
        if url.endswith("/Lucius-Morningstar/mailroom-producer"):
            return 200, json.dumps({"runtime": {"stage": "RUNNING"}})
        if url.endswith("/health"):
            return 200, json.dumps(
                {
                    "status": "ok",
                    "producer": True,
                    "review_resolve": True,
                    "inbox_upload": True,
                    "checks": {"watcher": "ok"},
                }
            )
        return 0, f"unexpected {url}"

    result = mod.probe(fetch_fn=fake_fetch)
    assert result["observatory"]["ok"] is True
    assert result["producer"]["ok"] is True
    assert result["paired"] is True
