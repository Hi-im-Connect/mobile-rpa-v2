# Mobile RPA v2: the agent runs on the phone

*2026-09-25. Status: design approved in chat ("go"), awaiting spec review.*

## Goal

Today the dashboard's Python worker (Mobilerun `MobileAgent`) drives each phone remotely: every
agent step pulls the screen layout and a screenshot from the phone and sends an action back. On a
weak link that costs seconds per trip (measured on the POCO F3 on 2026-09-25: a tiny request
3-5 s, one screenshot 10 s, about three trips per step).

v2 moves execution onto the phone:

- **Dashboard = planner and boss.** Task form, one prompt split across phones (editable preview),
  assignment, settings, history, per-phone spending.
- **App = executor.** Receives a task, runs the whole agent loop on the phone (read screen, ask the
  AI, act), and reports every step plus the result. Its own Run button runs the same loop.
- Nothing streams screenshots to the server during a task.

## Constraints (from the user)

- **A copy.** v1 (`~/claude_projects/mobile-rpa`, `~/claude_projects/fa-portal`, live at
  `/mobile/`) is not modified. v2 lives in new folders and runs side by side.
- **AI straight from the phone.** Each phone calls OpenRouter itself with its own capped key.
- No screen sharing on the phone (no MediaProjection). Screen reading = accessibility only.
- Small images only (JPEG, longest side 960 or less).

## 1. The copies

| | v1 (unchanged) | v2 |
|---|---|---|
| Dashboard repo | `~/claude_projects/mobile-rpa` | `~/claude_projects/mobile-rpa-v2` |
| App repo | `~/claude_projects/fa-portal` | `~/claude_projects/fa-portal-v2` |
| URL | `digimate.fastautomate.com/mobile/` | `digimate.fastautomate.com/mobile2/` |
| Server | CT 124, port 8090, `/var/lib/mobile-rpa` | CT 124, port 8091, `/var/lib/mobile-rpa-v2`, unit `mobile-rpa-v2` |
| App package | `com.mobilerun.portal` | `com.fastautomate.agent` (applicationId only; Kotlin namespace stays) |
| App name / link scheme | FastAutomate / `fastautomate://` | FastAutomate v2 / `fastautomate2://` (the `mobilerun`, `droidrun`, `fastautomate` schemes are removed in v2) |
| Phones | adb, NetBird, app | app only |

- v2 dashboard drops the adb/scrcpy/NetBird/QR-pairing modules and the Python agent worker. It
  keeps login, the FA theme, phones page, tasks page, history, settings, the splitter, app invites,
  and the on-demand watch view (silent accessibility screenshots, as v1 does now).
- The NPM proxy host gets one extra location `/mobile2` -> `192.168.100.44:8091`. The `/mobile`
  location is not touched.
- Both apps can be installed on one phone. Only one of them should have its accessibility service
  switched on at a time (the v2 connect page says so).
- Same FastAutomate signing key for both (`fa-portal/signing/`, copied, not moved).

## 2. Who does what

```
Dashboard v2 (planner)                        App v2 (executor)
  task form -> split per phone (LLM, main key)
  "agent/run" {run, instruction, settings,  ->  agent loop on the phone
               instructions for the agent}       observe (a11y tree, optional JPEG)
                                                 ask OpenRouter (phone's own key)
  step log, live on the task page          <-    act (tap/swipe/type/keys/open app)
  result + final screenshot                <-    "agent/finished"
```

- The existing reverse WebSocket carries everything. Dashboard -> app: JSON-RPC requests as today.
  App -> dashboard: messages with a `method` (events), which v1 already receives.
- **Dashboard-started run:** the dashboard creates task + run rows (each run has a `uuid`), then
  calls `agent/run`. The phone answers `{accepted: true}` or an error (busy, no key, accessibility
  off).
- **App-started run (its own Run button):** the app creates the uuid, runs at once, and sends
  `agent/started {uuid, instruction, origin: "app"}`. The dashboard creates the task/run rows when it
  arrives, even if that is later (after a reconnect).
- **Stop:** `agent/stop {uuid}` from the dashboard; the app's own Stop button does the same locally.

### Events (app -> dashboard)

`agent/event {uuid, seq, kind, text, ts}`, where kind is one of `phase`, `plan`, `think`, `action`,
`ok`, `error`, `answer` (the kinds v1's step log already shows).

`agent/finished {uuid, status: succeeded|failed|stopped, result, steps, shot}`: `shot` is one
480 px JPEG (base64) of the final screen.

- **Delivery survives disconnects.** The app keeps every event in an on-disk outbox until the
  dashboard replies `agent/ack {uuid, seq}`. On reconnect it resends everything not acked, in order.
  The dashboard stores events unique by `(uuid, seq)`, so repeats are harmless.
- The task keeps running on the phone when the connection drops, and when the dashboard restarts.

## 3. The agent on the phone (Kotlin, new package `com.mobilerun.portal.agent`)

Small units, each testable with fakes:

| Unit | Job |
|---|---|
| `ScreenReader` | Current screen as a compact indexed element list (index, class, text/description, resource id, bounds, clickable/editable/scrollable) from the app's existing accessibility tree; plus foreground app and keyboard state. Optional JPEG (960 px, quality 60) via the app's accessibility screenshot. |
| `LlmClient` | OpenAI-compatible `POST https://openrouter.ai/api/v1/chat/completions` over OkHttp, with tools (function calling), timeout 60 s, 2 retries on 429/5xx. Model, key and base URL come from settings. |
| `Actions` | Executes one tool call with the app's existing code: `tap(index)`, `tap_xy(x, y)`, `swipe(x1, y1, x2, y2)`, `type(text)`, `back()`, `home()`, `open_app(name)`, `wait(seconds)`, `done(success, answer)`. Points are kept on the screen. |
| `Planner` | With Reasoning on: the planner model writes up to 6 short goals at the start, and rewrites them after 3 failed actions in a row or the same action repeated 3 times. Reasoning off: no planner, the executor gets the task directly. |
| `AgentLoop` | One run: plan (optional) -> observe -> executor call -> act -> event, until `done`, `max_steps`, the time limit, or stop. One run at a time per phone. |
| `Outbox` | Events on disk, resent until acked. |
| `KeyStore` | The phone's OpenRouter key, encrypted with an Android Keystore AES-GCM key (no extra library). |

- **Instructions for the agent** (system prompts for planner and executor, tool descriptions) come
  from the dashboard with every `agent/run` and are saved on the phone. App-started runs use the
  last saved copy (a built-in copy ships for the first run). Changing agent behaviour therefore
  does not need an app update.
- **Settings sent with each run:** planner model, executor model, reasoning (default on), vision
  (default on), max steps (default 30), time limit (default 15 minutes), the same defaults as v1.
  App-started runs use the last settings received.
- The app's existing "Run an automation" card, task list and task details screens are reused. They
  read from the local agent instead of `/v1/tasks`.

## 4. Keys and money

- **Setup (once, by the user):** an OpenRouter **management key**
  (openrouter.ai/settings/management-keys) goes into v2 Settings. The normal API key stays there
  too; the dashboard uses it only for splitting tasks across phones.
- **Phone joins** -> the dashboard calls `POST /api/v1/keys` with
  `{name: "fastautomate-v2-<phone id>-<name>", limit: <daily cap>, limit_reset: "daily"}`, stores
  the returned key hash on the phone row, and sends key + hash once with `agent/credentials`. The key
  is never stored on the server.
- **Reconnects:** the app sends `X-Agent-Key-Hash` when it joins. If it matches the phone row,
  nothing happens. If it is missing or different, the old key is deleted and a new one issued.
- Daily cap per phone: Settings, default $2.00.
- **Phone removed** -> `DELETE /api/v1/keys/{hash}`. **Pause** button -> `PATCH {disabled: true}`,
  and Resume undoes it.
- **Spend on each card:** `GET /api/v1/keys/{hash}` -> `usage_daily`, refreshed when a run ends and
  every 10 minutes.

## 5. Watching and reports

- The task page shows each phone's steps as they arrive, the result, and the final screenshot.
- The phone card shows its current step while a run is going.
- The live watch view stays, on demand only (the v1 behaviour: silent accessibility screenshots,
  sent only when the screen changes). During a task nothing else leaves the phone except its own
  OpenRouter calls.

## 6. Errors

| Situation | Behaviour |
|---|---|
| Phone offline when a task is assigned | Not selectable (as in v1). |
| OpenRouter says the key's cap is reached (402/403) | Run ends `failed` with "This phone's daily AI budget is used up"; the card shows it. |
| No key yet / management key missing | Assigning is blocked with a message that says which setting to fill in. |
| Accessibility off on the phone | `agent/run` refused: "Turn on FastAutomate v2 in Accessibility". |
| An action fails on the phone | Reported as an `error` step; the loop continues. After 3 in a row the planner rewrites the goals, or, with Reasoning off, the run ends failed. |
| Dashboard unreachable mid-run | The run continues; events wait in the outbox. |

## 7. Testing

- **App (JUnit, fakes):** loop with a scripted fake LLM and a fake screen (done, max steps, stop,
  3 failures -> replan, repeated action -> replan); outbox resend and ack; element list formatting;
  on-screen clamping; key encryption round trip.
- **Dashboard (pytest):** `agent/run` dispatch; event intake (ordering, duplicates, late app-started
  runs); `agent/finished` storage; key create/delete/pause against a mocked OpenRouter management
  API; budget-exhausted message.
- **End to end:** the redroid (CT 101) with v1 and v2 apps both installed and a real model: one
  dashboard task, one app-started task, a mid-run disconnect, per-phone key created and deleted,
  and v1 still working unchanged. Needs OpenRouter credit (currently $0.01) and the management
  key.

## 8. Phases (each one usable on its own)

1. **Copies side by side.** Repos copied, v2 package, name, scheme and URLs; adb-era modules removed
   from the v2 dashboard; v2 deployed at `/mobile2/`. Done when both apps are installed on the
   redroid and v2 connects to v2 while v1 still works.
2. **Agent on the phone.** Per-phone keys, the Kotlin loop, dispatch, events with outbox, the
   result and final screenshot on the task page. Done when a dashboard task runs end to end on the
   redroid.
3. **Standalone and spending.** App-started runs synced to the dashboard, pause/resume, spend on
   the cards, the reused app screens, the POCO end-to-end check.

## Not in v2

- adb or NetBird phones, QR pairing, scrcpy video.
- Claude-format or other providers (only OpenRouter can issue capped per-phone keys).
- Running without the internet (each step needs an OpenRouter call).

## Addendum (2026-09-26, user request): other providers with one shared key

For testing, the user asked to run the agent on a company Gemini key (`gemini-3.1-flash-lite-preview`
through Gemini's OpenAI-compatible endpoint, `https://generativelanguage.googleapis.com/v1beta/openai`,
verified to return tool calls with `tool_choice: "required"`). So Settings has a provider URL:

- **OpenRouter** (default): unchanged, one capped key per phone from the management key.
- **Any other OpenAI-compatible provider**: every phone gets the dashboard's own API key through
  `agent/credentials` (hash `shared-...`). No per-phone cap, pause only stops new tasks, no spend
  figure, no account-credit check.
- Changing the provider or its key sends the new credentials to every connected phone.
