<p align="center">
  <img src="./docs/assets/fastautomate.png" width="120" alt="FastAutomate">
</p>

<h1 align="center">FastAutomate Mobile RPA</h1>

<p align="center">
  The dashboard for FastAutomate Mobile RPA: connect Android phones, give them tasks in plain words,
  and watch them do it live.<br>
  Works with the <a href="https://github.com/Hi-im-Connect/fa-portal-v2">FastAutomate v2 app</a>, which runs the AI agent on each phone.
</p>

---

## What it does

- **Connect phones in one tap.** Create an invite, open it on the phone, install the app. The download is
  stamped with a one-time token, so the phone joins by itself.
- **Write a task, pick phones, run.** Send one task to several phones at once, or let the dashboard split one
  prompt into a different job for each phone (for example, each phone installs a different app).
- **Watch live.** Every phone streams its screen; you can tap, swipe and type on it from the browser.
  Each run shows its plan and every step as it happens, with Pause, Resume and Stop.
- **Chats from the phones.** Conversations people have with the app's chat bubble show up in each phone's
  panel, next to the tasks those chats started.
- **AI keys handled for you.** With an OpenRouter management key, each phone gets its own key with a daily
  spending cap; or share one key (OpenRouter, or any OpenAI-compatible endpoint such as Gemini).

## How it works

The agent runs on the phone. The dashboard plans, assigns, records and shows.

```
 browser ──HTTP / live events──> dashboard (FastAPI + SQLite) <──WebSocket──> FastAutomate v2 app on each phone
                                                                              (planner + executor + chat layer)
```

- Dashboard to phone: `agent/run`, `agent/pause`, `agent/resume`, `agent/stop`, `agent/settings`, `agent/credentials`
- Phone to dashboard: `agent/started`, `agent/event`, `agent/finished`, `agent/chat`, each acknowledged with `agent/ack`

| Part | Where |
|---|---|
| Web app and API | `mobile_rpa/app.py` |
| Task runs, phone reports, watchdog | `mobile_rpa/orchestrator.py` |
| Storage (phones, tasks, runs, steps, chats, settings) | `mobile_rpa/db.py` (SQLite) |
| Invites and the stamped APK download | `mobile_rpa/appconnect.py`, `mobile_rpa/apk_stamp.py` |
| Per-phone AI keys | `mobile_rpa/phone_keys.py`, `mobile_rpa/openrouter_keys.py` |
| Browser UI | `mobile_rpa/static/` |

## Run it

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
MRPA_PASSWORD=choose-one .venv/bin/python -m mobile_rpa      # then open http://localhost:8090
.venv/bin/python -m pytest -q                                 # tests
```

| Setting | Default | Meaning |
|---|---|---|
| `MRPA_PASSWORD` | `connect` | dashboard login password (set your own) |
| `MRPA_DATA_DIR` | `./data` | where the SQLite database and screenshots live |
| `MRPA_PORT` | `8090` | web port |
| `MRPA_PUBLIC_URL` | FastAutomate's address | the address phones connect to |

Put the built app at `mobile_rpa/fa-portal-v2.apk` so invites can hand it out.
