"""Simulated live-production run driven through the real pager host (pipeline.pager.Pager.tick).

Snapshots are scripted (a 45-minute incident), everything else is real: config resolution,
the scan/digest state machine, rendering, delivery to every recipient, pager_log.jsonl.
Writes evidence/messages.json (what each phone received) + the JSON log."""
import json
import os
import sys
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

out_dir = sys.argv[1]
os.environ["MAILROOM_BASE_DIR"] = tempfile.mkdtemp()
os.environ.pop("MAILROOM_PAGER", None)

from pipeline import pager  # noqa: E402
from pipeline.pager_core import PagerConfig, ProviderStatus as PS, RunSnapshot  # noqa: E402
from pipeline.sms_notify import SendResult, count_segments  # noqa: E402

TZ = ZoneInfo("America/Chicago")
MOBILE, GV = "+16085551234", "+14145559876"


def at(h, m, s=0):
    return datetime(2026, 10, 15, h, m, s, tzinfo=TZ).timestamp()


class Phone:
    name = "twilio"

    def __init__(self):
        self.inbox = []

    def send(self, to, body):
        self.inbox.append({"to": to, "body": body, "at": self.now})
        return SendResult(True, "twilio", count_segments(body)[1], "SM" + "0" * 32)


GOOD = [PS("modal:fast", "up", "", 41, 0), PS("modal:extract", "up", "", 22, 0),
        PS("modal:vision", "idle"), PS("openrouter", "up", "$4.12 left", 9, 0)]
DOWN = [PS("modal:fast", "up", "", 38, 0), PS("modal:extract", "down", "HTTP 502", 6, 6),
        PS("modal:vision", "idle"), PS("openrouter", "up", "$4.08 left", 8, 0)]


def snapshot(now):
    t = now - at(14, 0)
    incident = at(14, 23) <= now < at(14, 36)
    fails = ([{"name": f"claim_{n:04d}.pdf", "error": "extraction timed out", "at": at(14, 22, 10 + 15 * k)}
              for k, n in enumerate((1840, 1841, 1842))] if incident else [])
    processed = 296 + int(t // 60)
    return RunSnapshot(
        taken_at=now, mode="production", watcher_alive=True, heartbeat_age_s=2.0, uptime_s=11520 + t,
        sha="6e3e006", queues={"inbox": 7 if incident else 2, "processing": 1, "review": 6, "failed": 2 + (3 if incident else 0)},
        window=({"minutes": 10, "received": 9, "archived": 4, "review": 1, "failed": 3, "p50_latency_s": 61.0,
                 "cost_usd": 0.12, "first_pass_rate": 0.44} if incident else
                {"minutes": 10, "received": 14, "archived": 11, "review": 2, "failed": 0, "p50_latency_s": 48.0,
                 "cost_usd": 0.21, "first_pass_rate": 0.79}),
        stuck=0, paused=None, outbox_dead=0, providers=DOWN if incident else GOOD, runs=[], failures=fails,
        openrouter={"limit_remaining": 4.12, "usage": 15.88, "is_free_tier": False},
        today={"processed": processed, "review": 9, "failed": 4 + (3 if incident else 0), "cost_usd": 4.86},
        host="ops-mac", repo="llm-mailroom")


pager.collect_snapshot = lambda cfg, window_minutes, now=None: snapshot(now)
cfg = PagerConfig.resolve({"enabled": True, "recipients": [MOBILE, GV], "timezone": "America/Chicago"}, {},
                          environment="live")
phone = Phone()
p = pager.Pager(cfg, transport=phone)
scans = 0
now = at(14, 0, 5)
while now < at(14, 45):
    phone.now = now
    p.tick(now)
    scans += 1
    now += 20  # host loop wakes every 20 s; scans run every scan.seconds (60)
log = [json.loads(line) for line in open(os.path.join(os.environ["MAILROOM_BASE_DIR"], "pager_log.jsonl"))]
seen, msgs = set(), []
for m in phone.inbox:
    if m["body"] not in seen:
        seen.add(m["body"])
        msgs.append({"at": datetime.fromtimestamp(m["at"], TZ).strftime("%-I:%M:%S %p"), "body": m["body"],
                     "recipients": sorted(x["to"] for x in phone.inbox if x["body"] == m["body"])})
json.dump({"ticks": scans, "texts_delivered": len(phone.inbox), "messages": msgs}, open(f"{out_dir}/messages.json", "w"),
          indent=1, ensure_ascii=False)
with open(f"{out_dir}/pager_log.jsonl", "w") as fh:
    for e in log:
        fh.write(json.dumps(e, ensure_ascii=False) + "\n")
print(f"ticks={scans} texts_delivered={len(phone.inbox)} distinct_messages={len(msgs)} log_entries={len(log)}")
for m in msgs:
    print(f"\n[{m['at']}] → {len(m['recipients'])} recipients\n{m['body']}")
