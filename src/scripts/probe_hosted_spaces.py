#!/usr/bin/env python3
"""Probe the live Hugging Face Observatory + producer pair.

Network-free ``--offline`` only prints the pinned Hub ids. Default mode
hits the public hosts (no tokens) and reports whether the Observatory
floor is up and whether a producer is reachable.

    PYTHONPATH=src python src/scripts/probe_hosted_spaces.py
    PYTHONPATH=src python src/scripts/probe_hosted_spaces.py --offline
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any

HUB_OBSERVATORY = "Lucius-Morningstar/mailroom-observatory"
HUB_PRODUCER = "Lucius-Morningstar/mailroom-producer"
OBSERVATORY_HOST = "https://lucius-morningstar-mailroom-observatory.hf.space"
PRODUCER_HOST = "https://lucius-morningstar-mailroom-producer.hf.space"
HUB_API = "https://huggingface.co/api/spaces"

TIMEOUT_S = 20


def fetch(
    url: str, *, timeout: float = TIMEOUT_S, method: str = "GET", data: bytes | None = None
) -> tuple[int, str]:
    req = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return int(resp.status), body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return int(exc.code), body
    except urllib.error.URLError as exc:
        return 0, str(exc.reason or exc)


def _json(body: str) -> Any:
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def probe(*, fetch_fn=fetch) -> dict[str, Any]:
    """Return a structured probe of both hosted Spaces."""
    hub_obs_code, hub_obs_body = fetch_fn(f"{HUB_API}/{HUB_OBSERVATORY}")
    hub_obs = _json(hub_obs_body) if hub_obs_code == 200 else None
    runtime = (hub_obs or {}).get("runtime") or {}

    health_code, health_body = fetch_fn(f"{OBSERVATORY_HOST}/api/health")
    health = _json(health_body) if health_code == 200 else None
    pipe_code, pipe_body = fetch_fn(f"{OBSERVATORY_HOST}/api/pipeline")
    pipeline = _json(pipe_body) if pipe_code == 200 else None

    prod_hub_code, _ = fetch_fn(f"{HUB_API}/{HUB_PRODUCER}")
    prod_health_code, prod_health_body = fetch_fn(f"{PRODUCER_HOST}/health")
    prod_health = _json(prod_health_body) if prod_health_code == 200 else None

    observatory_ok = bool(
        hub_obs_code == 200
        and runtime.get("stage") == "RUNNING"
        and health_code == 200
        and (health or {}).get("ok") is True
        and (health or {}).get("langfuse") is True
    )
    producer_ok = bool(
        prod_hub_code == 200
        and prod_health_code == 200
        and (prod_health or {}).get("status") in {"ok", "degraded"}
        and (prod_health or {}).get("producer") is True
    )
    paired = bool(
        observatory_ok
        and producer_ok
        and (pipeline or {}).get("configured") is True
    )

    return {
        "observatory": {
            "hub": HUB_OBSERVATORY,
            "hub_url": f"https://huggingface.co/spaces/{HUB_OBSERVATORY}",
            "host": OBSERVATORY_HOST,
            "hub_status": hub_obs_code,
            "runtime": runtime.get("stage"),
            "health_status": health_code,
            "ok": observatory_ok,
            "langfuse": (health or {}).get("langfuse"),
            "pipeline_configured": (health or {}).get("pipeline_configured"),
            "cached_trace_count": ((health or {}).get("cache") or {}).get(
                "cached_trace_count"
            ),
            "pipeline": pipeline,
        },
        "producer": {
            "hub": HUB_PRODUCER,
            "hub_url": f"https://huggingface.co/spaces/{HUB_PRODUCER}",
            "host": PRODUCER_HOST,
            "hub_status": prod_hub_code,
            "health_status": prod_health_code,
            "ok": producer_ok,
            "health": prod_health,
        },
        "paired": paired,
    }


def offline_pins() -> dict[str, str]:
    return {
        "observatory_hub": HUB_OBSERVATORY,
        "observatory_host": OBSERVATORY_HOST,
        "producer_hub": HUB_PRODUCER,
        "producer_host": PRODUCER_HOST,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="print pinned Hub ids only; no network",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    if args.offline:
        pins = offline_pins()
        if args.json:
            print(json.dumps(pins, indent=2))
        else:
            for key, value in pins.items():
                print(f"{key} {value}")
        return 0

    result = probe()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        obs = result["observatory"]
        prod = result["producer"]
        print(
            f"observatory {obs['hub']} runtime={obs['runtime']} "
            f"health={obs['health_status']} ok={obs['ok']} "
            f"langfuse={obs['langfuse']} "
            f"pipeline_configured={obs['pipeline_configured']} "
            f"traces={obs['cached_trace_count']}"
        )
        print(
            f"producer {prod['hub']} hub={prod['hub_status']} "
            f"health={prod['health_status']} ok={prod['ok']}"
        )
        print(f"paired {result['paired']}")
        print(obs["hub_url"])
        print(prod["hub_url"])

    if not result["observatory"]["ok"]:
        return 2
    if not result["producer"]["ok"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
