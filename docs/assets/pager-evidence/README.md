# Operator SMS pager — verification evidence

What an operator's phone receives during a live production run. The texts are
produced by the real pager host (`pipeline.pager.Pager.tick`): real config
resolution, real scan/digest scheduling, real rendering, delivery to every
recipient and the real `pager_log.jsonl`. Only the run snapshots (a scripted
45-minute incident) and the phone (a recording transport in place of Twilio)
are simulated.

Scenario (2:00–2:45 PM CDT, two recipients, 135 host ticks at 20 s):

| Time | Event | Text |
|---|---|---|
| 2:10, 2:20 | healthy | 🟢 digest |
| 2:23 | Modal extract tier starts failing (HTTP 502) | 🔴 ALERT |
| 2:24 | 3 documents failed | 🟠 ALERT |
| 2:30 | incident still open | digest that opens with the open problem |
| 2:37 | extract recovers, failures stop | ✅ RESOLVED (both recoveries in one text) |
| 2:40 | healthy again | 🟢 digest |

The 60-second scans between those times sent nothing because the run was healthy.
The run sent 7 distinct texts and made 14 deliveries (7 × 2 recipients).

| File | What it is |
|---|---|
| `01-live-digest-healthy.png` | routine 10-minute digest |
| `02-alert-modal-down.png` | critical alert while Modal is down |
| `03-digest-open-alert.png` | digest during the incident |
| `04-resolved-all-clear.png` | recovery text and the next all-clear digest |
| `05-full-incident-thread.png` | the whole thread as one phone shows it |
| `simulation-transcript.txt` | the exact text bodies, in order |
| `messages.json` | what each phone received (numbers masked) |
| `pager_log.jsonl` | the machine-readable `mailroom.pager/v1` record of every send |
| `simulate_live_run.py`, `render_phone.py` | scripts that reproduce the above |

Reproduce:

```bash
PYTHONPATH=src .venv/bin/python docs/assets/pager-evidence/simulate_live_run.py /tmp/out
python docs/assets/pager-evidence/render_phone.py /tmp/out/messages.json /tmp/out/thread.html "Mailroom" 0,1,2,3,4,5,6
```
