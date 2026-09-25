# Mobile RPA v2 (agent on the phone) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build v2 copies of the Mobile RPA dashboard and the FastAutomate app in which the app runs the whole agent loop on its own phone, the dashboard only plans, assigns and reports, and every phone pays for AI with its own capped OpenRouter key.

**Architecture:** Dashboard v2 is a copy of the v1 FastAPI app. Its server-side Mobilerun worker (and everything adb) is replaced by an `Orchestrator` that sends `agent/run` over each phone's existing WebSocket and stores the `agent/*` reports the phone sends back (acked, de-duplicated). App v2 is a copy of the FastAutomate app with a new `agent` package: a Kotlin loop that reads the accessibility tree, asks OpenRouter for exactly one tool call, and performs it through the app's own `ActionDispatcher`, keeping reports in an on-disk outbox until the dashboard acks them.

**Tech Stack:** Python 3.13, FastAPI, SQLite, httpx, pytest + pytest-asyncio (auto mode); Kotlin 1.9, Android SDK 34 (minSdk 26), OkHttp 4.12, org.json, JUnit 4, mockk; OpenRouter chat completions and key-management API.

**Spec:** `/home/arrow/claude_projects/mobile-rpa-v2/docs/superpowers/specs/2026-09-25-on-phone-agent-design.md`

## Global Constraints

- **v1 stays untouched.** Never edit `~/claude_projects/mobile-rpa` or `~/claude_projects/fa-portal`, the v1 service `mobile-rpa` (CT 124, port 8090, `/opt/mobile-rpa`, `/var/lib/mobile-rpa`) or the NPM `/mobile` location. Never run `adb kill-server` on CT 124 (v1 shares that adb daemon).
- **v2 locations:**
  - dashboard repo `~/claude_projects/mobile-rpa-v2` (Python package still named `mobile_rpa`)
  - app repo `~/claude_projects/fa-portal-v2`
  - server: CT 124, port 8091, code `/opt/mobile-rpa-v2`, data `/var/lib/mobile-rpa-v2`, systemd unit `mobile-rpa-v2`
  - public URL `https://digimate.fastautomate.com/mobile2`
- **v2 app identity:** applicationId `com.fastautomate.agent` (the Kotlin namespace stays `com.mobilerun.portal`), label `FastAutomate v2`, link scheme `fastautomate2` only, APK download `/app/FastAutomate-v2.apk`, local file `mobile_rpa/fa-portal-v2.apk`.
- **No screen sharing:** no MediaProjection anywhere in v2. Screenshots are accessibility JPEGs: 960 px on the longest side (quality 60) for vision, 480 px (quality 60) for the final report.
- **OpenRouter only.** Base URL `https://openrouter.ai/api/v1`; key management at `https://openrouter.ai/api/v1/keys`.
- **Management key:** never written to any file, git or memory. It enters only through v2 Settings (the UI, or `PUT /mobile2/api/settings` with a login cookie).
- **Defaults:** max steps 30, time limit 15 minutes, reasoning on, vision on, daily cap $2.00 per phone, models `google/gemini-2.5-flash`.
- **Protocol:**
  - dashboard to app: `agent/run`, `agent/stop`, `agent/credentials`, `agent/ack`
  - app to dashboard: `agent/started`, `agent/event`, `agent/finished`, each carrying `uuid` and a per-run `seq` (0 for `agent/started`)
  - join header: `X-Agent-Key-Hash`
- **Agent tools:** `tap`, `tap_at`, `type`, `scroll`, `back`, `home`, `enter`, `open_app`, `wait`, `done`, with the same names and arguments in `agent_prompts.py` and `Actions.kt`.
- **Step-log kinds:** `phase`, `plan`, `think`, `action`, `ok`, `error`, `answer`. The dashboard adds `done` or `fail` with the final result, as v1 does.
- **Commits:** the user is the sole author, so no `Co-Authored-By` or other AI trailers. Plain `git commit -m`.

## Review Focus

1. **A phone that reconnects mid-run resends reports it already sent.** Each `(uuid, seq)` is stored once and both copies are acked. Test `test_duplicate_events_are_stored_once` in Task 8.
2. **The model answers with plain text, unreadable arguments or an unknown tool.** That is one `error` step and the loop continues. Test `model without a tool call is a failed step` in Task 16.
3. **The screen changed between reading and acting, so the element number no longer exists.** An `error` step "Element 42 is not on the screen", no crash. Test `missing element is a failed step` in Task 15.
4. **The phone's daily cap or the account credit runs out mid-run** (OpenRouter 403 key limit or 402). The run ends failed with a readable budget message. Tests in Task 14 (`key limit is a budget error`) and Task 16 (`budget exhaustion fails the run`).
5. **Reports arrive for a run the dashboard does not know** (a wiped DB, or `agent/event` without a stored `agent/started`). They are acked and dropped with a log line, never a crash. Test `test_unknown_run_events_are_acked_and_dropped` in Task 8.

## File map

**Dashboard v2 (`~/claude_projects/mobile-rpa-v2`)**

| File | Responsibility | Phase |
|---|---|---|
| `mobile_rpa/settings.py` | env defaults for v2; runtime settings (OpenRouter-only, management key, daily cap) | 1, 2 |
| `mobile_rpa/appconnect.py` | connect pages, APK download, the app's WebSocket join (+ key handshake); `/v1/tasks` REST removed in phase 3 | 1, 2, 3 |
| `mobile_rpa/openrouter_keys.py` (new) | `KeyManager`: OpenRouter key-management calls | 2 |
| `mobile_rpa/phone_keys.py` (new) | `PhoneKeys`: one capped key per phone (create, replace, forget, pause, spend) | 2, 3 |
| `mobile_rpa/agent_prompts.py` (new) | planner/executor instructions, tool schemas, `run_payload()`, `defaults_json()` | 2, 3 |
| `mobile_rpa/orchestrator.py` (new) | sends `agent/run`/`agent/stop`, stores `agent/*` reports, acks, watchdogs | 2 |
| `mobile_rpa/db.py` | runs.uuid/origin, run_events.seq (unique), phones.key_hash/paused | 2 |
| `mobile_rpa/devices.py` | `DeviceConn.notify`, `DeviceHub.on_message`, `serve(..., on_ready=)` | 2 |
| `mobile_rpa/phones.py` | app-only phone registry | 2 |
| `mobile_rpa/app.py` | routes; wires orchestrator + keys; no adb/scrcpy/pairing/runner | 2, 3 |
| `mobile_rpa/splitter.py` | OpenAI-format only | 2 |
| `mobile_rpa/static/app.js`, `index.html` | app-only add-phone, v2 settings, tiles without video, final shots, spend/pause | 1, 2, 3 |
| deleted in phase 2 | `adb.py`, `scrcpy.py`, `scrcpy-server-v4.1.jar`, `pairing.py`, `runner.py`, `worker.py`, their tests, `tests/fake_worker.py` | 2 |
| `deploy/deploy.sh`, `deploy/mobile-rpa-v2.service`, `deploy/npm_add_mobile2.js` | ship v2 to CT 124 port 8091, add NPM `/mobile2` | 1 |

**App v2 (`~/claude_projects/fa-portal-v2`, package dir `app/src/main/java/com/mobilerun/portal/`)**

| File | Responsibility | Phase |
|---|---|---|
| `app/build.gradle.kts`, `AndroidManifest.xml`, `res/values/strings.xml`, `build-fa.sh` | v2 identity | 1 |
| `taskprompt/PortalAuthDeepLink.kt`, `config/ConfigManager.kt` | v2 URLs and scheme | 1 |
| `agent/Model.kt` | data types: `Element`, `Screen`, `Prompts`, `RunSpec`, `ToolCall`, `ActionOutcome`, `RunStatus`, `EventSink` | 2 |
| `agent/ScreenReader.kt` | accessibility state JSON to an indexed element list and its text form | 2 |
| `agent/PhoneControl.kt` | `PhoneControl` interface + `DispatcherPhoneControl` (local `ActionDispatcher`) | 2 |
| `agent/LlmClient.kt` | OpenRouter chat completions with tools; `LlmTransport` + `OkHttpTransport` | 2 |
| `agent/Actions.kt` | performs one tool call | 2 |
| `agent/Planner.kt` | goals from the planner model | 2 |
| `agent/AgentLoop.kt` | one run: plan, observe, decide, act, report | 2 |
| `agent/Outbox.kt` | reports on disk until acked | 2 |
| `agent/KeyVault.kt` | the phone's key, sealed with an Android Keystore AES-GCM key | 2 |
| `agent/AgentHost.kt` | one run at a time, report numbering, `agent/*` message handling; `AgentRuntime` singleton | 2, 3 |
| `agent/RunStore.kt` | local history of runs for the app's own screens | 3 |
| `agent/LocalTasks.kt` | answers the app's task screens from `RunStore`/`AgentHost` | 3 |
| `service/ActionDispatcher.kt`, `service/HeadlessActionSupport.kt`, `service/ReverseConnectionService.kt`, `PortalApplication.kt` | wiring | 2 |
| `taskprompt/PortalCloudClient.kt` | task-screen calls go to `LocalTasks` | 3 |

---

# Phase 1: the copies side by side

### Task 1: Dashboard v2 copy with v2 identity and its own deployment

**Files:**
- Create: everything under `~/claude_projects/mobile-rpa-v2/` except `docs/` (already there), copied from v1
- Modify: `mobile_rpa/settings.py`, `mobile_rpa/appconnect.py`, `pyproject.toml`, `.gitignore`, `README.md`
- Create: `deploy/mobile-rpa-v2.service`, `deploy/deploy.sh` (replaces the copied one), `tests/test_v2_identity.py`
- Delete: `deploy/mobile-rpa.service`, `deploy/netbird-peers-api/`

**Interfaces:**
- Produces: `appconnect.APP_PACKAGE == "com.fastautomate.agent"`, `appconnect.CALLBACK_SCHEMES == {"fastautomate2"}`, `appconnect.APK` named `fa-portal-v2.apk`, the route `GET /app/FastAutomate-v2.apk`, `load_env().public_url == "https://digimate.fastautomate.com/mobile2"`.

- [ ] **Step 1: Copy the v1 tree (the v1 folder is only read)**

```bash
rsync -a --exclude .git --exclude .venv --exclude data --exclude __pycache__ --exclude .pytest_cache \
  --exclude .ruff_cache --exclude mobile_rpa.egg-info --exclude docs --exclude 'mobile_rpa/fa-portal.apk' \
  ~/claude_projects/mobile-rpa/ ~/claude_projects/mobile-rpa-v2/
cd ~/claude_projects/mobile-rpa-v2 && rm -rf deploy/netbird-peers-api deploy/mobile-rpa.service
uv venv -q -p 3.13 .venv && uv pip install -q -p .venv -e '.[dev]'
.venv/bin/python -m pytest -q 2>&1 | tail -1
```
Expected: `58 passed` (the copy behaves like v1).

- [ ] **Step 2: Write the failing identity test**

`tests/test_v2_identity.py`:
```python
from mobile_rpa import appconnect
from mobile_rpa.settings import load_env

from .test_app import client, login  # noqa: F401  (fixture + helper)


def test_v2_identity(monkeypatch, tmp_path):
    monkeypatch.setenv("MRPA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("MRPA_PUBLIC_URL", raising=False)
    assert load_env().public_url == "https://digimate.fastautomate.com/mobile2"
    assert appconnect.APP_PACKAGE == "com.fastautomate.agent"
    assert appconnect.CALLBACK_SCHEMES == {"fastautomate2"}
    assert appconnect.APK.name == "fa-portal-v2.apk"


def test_connect_page_opens_the_v2_app(client):  # noqa: F811
    login(client)
    url = client.post("/api/app/invite").json()["url"]
    page = client.get("/connect?t=" + url.split("t=")[1]).text
    assert "fastautomate2://connect" in page
    assert "scheme=fastautomate2;package=com.fastautomate.agent" in page
    assert "/app/FastAutomate-v2.apk" in page
    assert client.get("/connect/device?deviceId=d1").text.count('value="fastautomate2"') == 1
```

- [ ] **Step 3: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_v2_identity.py -q`
Expected: FAIL (`public_url` is `.../mobile`, `APP_PACKAGE` is `com.mobilerun.portal`).

- [ ] **Step 4: Give the copy its v2 identity**

```bash
cd ~/claude_projects/mobile-rpa-v2 && python3 - <<'PY'
import re
def edit(path, pairs):
    s = open(path).read()
    for a, b in pairs:
        assert a in s, (path, a)
        s = s.replace(a, b)
    open(path, "w").write(s)

edit("mobile_rpa/settings.py", [
    ('public_url: str = "https://digimate.fastautomate.com/mobile"', 'public_url: str = "https://digimate.fastautomate.com/mobile2"'),
    ('os.environ.get("MRPA_PUBLIC_URL", "https://digimate.fastautomate.com/mobile")', 'os.environ.get("MRPA_PUBLIC_URL", "https://digimate.fastautomate.com/mobile2")'),
])
edit("mobile_rpa/appconnect.py", [
    ('APK = Path(__file__).with_name("fa-portal.apk")', 'APK = Path(__file__).with_name("fa-portal-v2.apk")'),
    ('CALLBACK_SCHEMES = {"fastautomate", "droidrun", "mobilerun"}', 'CALLBACK_SCHEMES = {"fastautomate2"}'),
    ('APP_PACKAGE = "com.mobilerun.portal"', 'APP_PACKAGE = "com.fastautomate.agent"'),
    ('@app.get("/app/FastAutomate.apk")', '@app.get("/app/FastAutomate-v2.apk")'),
    ('filename="FastAutomate.apk")', 'filename="FastAutomate-v2.apk")'),
    ('link = f"fastautomate://connect?', 'link = f"fastautomate2://connect?'),
    ('#Intent;scheme=fastautomate;package={APP_PACKAGE};end")\n        body = f"""\n        <ol',
     '#Intent;scheme=fastautomate2;package={APP_PACKAGE};end")\n        body = f"""\n        <ol'),
    ('href="{public}/app/FastAutomate.apk">Download FastAutomate</a>', 'href="{public}/app/FastAutomate-v2.apk">Download FastAutomate v2</a>'),
    ('<li><b>Turn on the FastAutomate service</b><span>In the app, tap <i>Accessibility Service</i> and switch it on.',
     '<li><b>Turn on the FastAutomate v2 service</b><span>In the app, tap <i>Accessibility Service</i> and switch it on. If the first FastAutomate app is also on this phone, switch its service off.'),
    ('Settings &gt; Apps &gt; FastAutomate &gt;', 'Settings &gt; Apps &gt; FastAutomate v2 &gt;'),
    ('async def device_login_page(deviceId: str = "", scheme: str = "fastautomate"):', 'async def device_login_page(deviceId: str = "", scheme: str = "fastautomate2"):'),
    ('scheme = str(form.get("scheme", "fastautomate"))', 'scheme = str(form.get("scheme", "fastautomate2"))'),
    ('            scheme = "fastautomate"\n', '            scheme = "fastautomate2"\n'),
    ('>Open FastAutomate</a>', '>Open FastAutomate v2</a>'),
    ('"download": f"{public}/app/FastAutomate.apk"}', '"download": f"{public}/app/FastAutomate-v2.apk"}'),
])
edit("pyproject.toml", [
    ('name = "mobile-rpa"', 'name = "mobile-rpa-v2"'),
    ('description = "Mobile RPA dashboard: connect Android phones, watch them live, run LLM tasks on them"',
     'description = "Mobile RPA v2: the FastAutomate v2 app runs tasks on the phone; this dashboard plans and reports"'),
])
PY
printf '.venv/\ndata/\n__pycache__/\n*.egg-info/\n.pytest_cache/\nmobile_rpa/fa-portal-v2.apk\n' > .gitignore
cat > README.md <<'MD'
# Mobile RPA v2

Copy of Mobile RPA (v1 stays live at /mobile/) where the FastAutomate v2 app runs the agent on the
phone and this dashboard plans, assigns and reports. Live at https://digimate.fastautomate.com/mobile2/.

- Tests: `.venv/bin/python -m pytest -q`
- Deploy: `./deploy/deploy.sh` (CT 124, port 8091, data /var/lib/mobile-rpa-v2)
- Design: docs/superpowers/specs/2026-09-25-on-phone-agent-design.md
MD
```

- [ ] **Step 5: Add the v2 unit and deploy script**

`deploy/mobile-rpa-v2.service`:
```ini
[Unit]
Description=Mobile RPA v2 dashboard (agent on the phone)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/mobile-rpa-v2
Environment=MRPA_DATA_DIR=/var/lib/mobile-rpa-v2
Environment=MRPA_HOST=0.0.0.0
Environment=MRPA_PORT=8091
Environment=MRPA_PUBLIC_URL=https://digimate.fastautomate.com/mobile2
Environment=MRPA_ADB=/usr/local/bin/adb
Environment=PATH=/usr/local/bin:/usr/bin:/bin
Environment=MRPA_PASSWORD=connect
Environment=HOME=/var/lib/mobile-rpa-v2
ExecStart=/opt/mobile-rpa-v2/.venv/bin/python -m mobile_rpa
LimitNOFILE=8192
Restart=always
RestartSec=3
KillMode=mixed
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
```
(`MRPA_ADB` stays only until phase 2 removes adb. There is no `adb start-server` and no route line: v1 owns the adb daemon.)

`deploy/deploy.sh`:
```bash
#!/usr/bin/env bash
# Ship v2 to FA LXC 124 next to v1 and restart only mobile-rpa-v2. v1 (/opt/mobile-rpa, port 8090) is not touched.
set -euo pipefail
NODE=root@192.168.100.20
KEY=~/.ssh/id_root
CT=124
cd "$(dirname "$0")/.."
tar czf /tmp/mobile-rpa-v2.tgz --exclude=.venv --exclude=data --exclude=.git --exclude=__pycache__ --exclude=.pytest_cache --exclude=docs .
scp -q -i $KEY /tmp/mobile-rpa-v2.tgz deploy/mobile-rpa-v2.service $NODE:/tmp/
ssh -i $KEY $NODE "pct push $CT /tmp/mobile-rpa-v2.tgz /tmp/mobile-rpa-v2.tgz && pct push $CT /tmp/mobile-rpa-v2.service /etc/systemd/system/mobile-rpa-v2.service && pct exec $CT -- bash -c '
  set -e; export PATH=/usr/local/bin:\$PATH
  mkdir -p /opt/mobile-rpa-v2 /var/lib/mobile-rpa-v2 && chmod 700 /var/lib/mobile-rpa-v2
  tar xzf /tmp/mobile-rpa-v2.tgz -C /opt/mobile-rpa-v2
  cd /opt/mobile-rpa-v2
  [ -d .venv ] || uv venv -q -p 3.13 .venv
  uv pip install -q -p .venv -e .
  systemctl daemon-reload && systemctl enable -q mobile-rpa-v2 && systemctl restart mobile-rpa-v2
  sleep 3 && systemctl is-active mobile-rpa-v2 && curl -fsS -o /dev/null -w \"http %{http_code}\n\" http://127.0.0.1:8091/
'"
```
Run `chmod +x deploy/deploy.sh`.

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass (60 = 58 + 2). (No v1 test pins the old scheme, package or APK name.)

- [ ] **Step 7: Commit**

```bash
cd ~/claude_projects/mobile-rpa-v2 && git add -A && git commit -q -m "v2 dashboard: copy of v1 with its own identity (/mobile2, com.fastautomate.agent, fastautomate2) and deployment (port 8091)"
```

### Task 2: Dashboard v2 adds phones through the app only

**Files:**
- Modify: `mobile_rpa/static/app.js` (the `addPhone` function, the `v-prepare` handler), `mobile_rpa/static/index.html` (the `v-prepare` button, header)
- Test: `tests/test_static.py`

**Interfaces:**
- Consumes: `POST api/app/invite` → `{url, download}` (Task 1).

- [ ] **Step 1: Write the failing test** (append to `tests/test_static.py`)

```python
def test_v2_adds_phones_through_the_app_only():
    js = (STATIC / "app.js").read_text()
    html = (STATIC / "index.html").read_text()
    for gone in ("api/pair/qr", "api('api/phones', {method: 'POST'", "/prepare", 'data-m="qr"', 'data-m="code"', 'data-m="addr"'):
        assert gone not in js, gone
    assert "v-prepare" not in html and "v-prepare" not in js
    assert "FastAutomate v2" in js
    assert ">v2<" in html
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_static.py -q`
Expected: FAIL on `api/pair/qr`.

- [ ] **Step 3: Replace `addPhone` in `app.js`**

Replace the whole `function addPhone() { ... }` (from `function addPhone() {` up to the line before `/* ================= live viewer ================= */`) with:
```js
function addPhone() {
  openModal({
    title: 'Add a phone',
    sub: 'Works on any internet: no cable, no Wi-Fi setup, no developer options.',
    body: `<div class="qr-row">
        <div class="qr" id="m-appqr">Loading...</div>
        <ol class="steps-list">
          <li>Scan this with the phone's <b>camera</b>.</li>
          <li>Install <b>FastAutomate v2</b>, then tap <b>Connect</b>.</li>
          <li>Turn on the app's service. The phone appears here.</li>
        </ol>
      </div>
      <p class="field-note">Already have FastAutomate v2? Tap <b>Connect with password</b> in it.
        Link: <a id="m-applink" target="_blank" rel="noopener"></a></p>`,
    go: 'Close',
    submit: async () => {},
  });
  const knownIds = new Set(S.phones.map((p) => p.id));
  api('api/app/invite', {method: 'POST'}).then(async (r) => {
    $('m-applink').textContent = r.url.replace(/^https?:\/\//, ''); $('m-applink').href = r.url;
    $('m-appqr').innerHTML = (await api('api/qr?text=' + encodeURIComponent(r.url))).svg;
  }).catch((e) => { $('m-appqr').textContent = 'Unavailable: ' + e.message; });
  // the window closes by itself when the phone connects
  const watchNew = () => {
    if (!$('modal-wrap').classList.contains('on')) return;
    const fresh = S.phones.find((p) => !knownIds.has(p.id));
    if (fresh) { closeModal(); toast(fresh.name + ' connected'); return; }
    setTimeout(watchNew, 1000);
  };
  watchNew();
}
```
Delete the `$('v-prepare').onclick = async () => { ... };` block (3 lines).

- [ ] **Step 4: Edit `index.html`**

Delete the element with `id="v-prepare"` (the "Repair Portal" button in the viewer). In the header, right after the product name text `Mobile RPA`, add `<span class="tag online" style="margin-left:8px">v2</span>`. Change `<title>` to `Mobile RPA v2`.

- [ ] **Step 5: Run all tests**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass. The existing "every button id is used" test must still pass; if it names `v-prepare`, delete that name from its list.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -q -m "v2 dashboard: phones join through the FastAutomate v2 app only"
```

### Task 3: App v2 copy with its own package, name, scheme and server

**Files:**
- Create: `~/claude_projects/fa-portal-v2/` copied from `~/claude_projects/fa-portal`
- Modify: `app/build.gradle.kts:11`, `app/src/main/AndroidManifest.xml` (the auth/connect `<data>` entries), `app/src/main/res/values/strings.xml` (`app_name`, `keyboard_name`, `keyboard_on`), `app/src/main/java/com/mobilerun/portal/taskprompt/PortalAuthDeepLink.kt`, `app/src/main/java/com/mobilerun/portal/config/ConfigManager.kt:104-105`, `build-fa.sh`
- Test: `app/src/test/java/com/mobilerun/portal/V2IdentityTest.kt`, plus the copied tests that assert v1 URLs (`PortalAuthCallbackValidatorTest`, `PortalCloudClientTest`)

**Interfaces:**
- Produces: `ConfigManager.DEFAULT_REVERSE_CONNECTION_URL` (now public) = `wss://digimate.fastautomate.com/mobile2/v1/providers/personal/join`; `PortalAuthDeepLink.PREFERRED_CALLBACK_SCHEME == "fastautomate2"`; APK at `dist/fa-portal-v2.apk`.

- [ ] **Step 1: Copy (the SDK is shared through a symlink; the signing key is copied)**

```bash
rsync -a --exclude .git --exclude .gradle --exclude build --exclude app/build --exclude dist --exclude .android-sdk \
  ~/claude_projects/fa-portal/ ~/claude_projects/fa-portal-v2/
cd ~/claude_projects/fa-portal-v2 && ln -s ../fa-portal/.android-sdk .android-sdk && git init -q
ls signing/fa-portal.jks signing/keystore.env
```
Expected: both signing files listed.

- [ ] **Step 2: Write the failing identity test**

`app/src/test/java/com/mobilerun/portal/V2IdentityTest.kt`:
```kotlin
package com.mobilerun.portal

import com.mobilerun.portal.config.ConfigManager
import com.mobilerun.portal.taskprompt.PortalAuthDeepLink
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class V2IdentityTest {
    @Test
    fun `v2 talks to the v2 dashboard`() {
        assertEquals(
            "wss://digimate.fastautomate.com/mobile2/v1/providers/personal/join",
            ConfigManager.DEFAULT_REVERSE_CONNECTION_URL,
        )
        assertTrue(
            PortalAuthDeepLink.buildCloudLoginUrl("d1", false)
                .startsWith("https://digimate.fastautomate.com/mobile2/connect/device?deviceId=d1&scheme=fastautomate2"),
        )
    }

    @Test
    fun `only the fastautomate2 scheme is accepted`() {
        assertEquals("fastautomate2", PortalAuthDeepLink.PREFERRED_CALLBACK_SCHEME)
        assertTrue(PortalAuthDeepLink.isConnectLink("fastautomate2", "connect"))
        assertFalse(PortalAuthDeepLink.isConnectLink("fastautomate", "connect"))
        assertFalse(PortalAuthDeepLink.isAuthCallback("mobilerun", "auth-callback"))
        assertTrue(PortalAuthDeepLink.isAuthCallback("fastautomate2", "auth-callback"))
    }
}
```

- [ ] **Step 3: Run it to verify it fails**

Run: `cd ~/claude_projects/fa-portal-v2 && export JAVA_HOME=$(mise where java@temurin-17) ANDROID_HOME=$PWD/.android-sdk && ./gradlew -q testDebugUnitTest --tests '*V2IdentityTest'`
Expected: compile error (`DEFAULT_REVERSE_CONNECTION_URL` is private).

- [ ] **Step 4: Apply the v2 identity**

```bash
cd ~/claude_projects/fa-portal-v2 && python3 - <<'PY'
def edit(path, pairs):
    s = open(path).read()
    for a, b in pairs:
        assert a in s, (path, a)
        s = s.replace(a, b)
    open(path, "w").write(s)
J = "app/src/main/java/com/mobilerun/portal/"
edit("app/build.gradle.kts", [('applicationId = "com.mobilerun.portal"', 'applicationId = "com.fastautomate.agent"')])
edit("app/src/main/res/values/strings.xml", [
    ('<string name="app_name">FastAutomate</string>', '<string name="app_name">FastAutomate v2</string>'),
    ('<string name="keyboard_name">FastAutomate Keyboard</string>', '<string name="keyboard_name">FastAutomate v2 Keyboard</string>'),
    ('<string name="keyboard_on">FastAutomate Keyboard</string>', '<string name="keyboard_on">FastAutomate v2 Keyboard</string>'),
])
edit(J + "taskprompt/PortalAuthDeepLink.kt", [
    ('const val PREFERRED_CALLBACK_SCHEME = "fastautomate"', 'const val PREFERRED_CALLBACK_SCHEME = "fastautomate2"'),
    ('private const val LEGACY_CALLBACK_SCHEME = "mobilerun"\n', ''),
    ('"https://digimate.fastautomate.com/mobile/connect/device"', '"https://digimate.fastautomate.com/mobile2/connect/device"'),
    ('// QR / link from the dashboard: fastautomate://connect', '// QR / link from the dashboard: fastautomate2://connect'),
    ('setOf(PREFERRED_CALLBACK_SCHEME, LEGACY_CALLBACK_SCHEME, "droidrun")', 'setOf(PREFERRED_CALLBACK_SCHEME)'),
])
edit(J + "config/ConfigManager.kt", [
    ('private const val DEFAULT_REVERSE_CONNECTION_URL =\n            "wss://digimate.fastautomate.com/mobile/v1/providers/personal/join"',
     'const val DEFAULT_REVERSE_CONNECTION_URL =\n            "wss://digimate.fastautomate.com/mobile2/v1/providers/personal/join"'),
])
m = "app/src/main/AndroidManifest.xml"
s = open(m).read()
for scheme, host in (("mobilerun", "auth-callback"), ("droidrun", "auth-callback")):
    line = f'android:scheme="{scheme}"\n                    android:host="{host}" />'
    assert line in s, line
    start = s.rindex("<data", 0, s.index(line))
    s = s[:start] + s[s.index(line) + len(line):]
s = s.replace('android:scheme="fastautomate"\n', 'android:scheme="fastautomate2"\n')
open(m, "w").write(s)
edit("build-fa.sh", [("dist/fa-portal.apk", "dist/fa-portal-v2.apk")] )
PY
grep -n 'android:scheme' app/src/main/AndroidManifest.xml
```
Expected: exactly two `android:scheme="fastautomate2"` lines (hosts `auth-callback` and `connect`). If blank lines remain where the removed `<data>` entries were, delete them.

- [ ] **Step 5: Update the copied tests that pin v1 values**

In `PortalAuthCallbackValidatorTest.kt`, replace `https://digimate.fastautomate.com/mobile/connect/device?deviceId=device-123&scheme=fastautomate` with `https://digimate.fastautomate.com/mobile2/connect/device?deviceId=device-123&scheme=fastautomate2` (two places). In `PortalCloudClientTest.kt`, replace `wss://digimate.fastautomate.com/mobile/v1/providers/personal/join` with `wss://digimate.fastautomate.com/mobile2/v1/providers/personal/join` and `"https://digimate.fastautomate.com/mobile"` with `"https://digimate.fastautomate.com/mobile2"`. Then run `grep -rn 'fastautomate.com/mobile[^2]\|"fastautomate"' app/src/test` and fix any remaining v1 value the same way.

- [ ] **Step 6: Run all unit tests and build**

Run: `./gradlew -q testDebugUnitTest && ./build-fa.sh`
Expected: tests pass; the build ends with `dist/fa-portal-v2.apk`. Then:
```bash
.android-sdk/build-tools/35.0.0/aapt2 dump badging dist/fa-portal-v2.apk | grep -E "^package|application-label:"
```
Expected: `package: name='com.fastautomate.agent'` and `application-label:'FastAutomate v2'`.

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -q -m "FastAutomate v2 app: copy with its own package (com.fastautomate.agent), name, fastautomate2 scheme and the /mobile2 dashboard"
```

### Task 4: Deploy v2 next to v1 and connect the redroid through the v2 app

**Files:**
- Create: `~/claude_projects/mobile-rpa-v2/deploy/npm_add_mobile2.js`
- Uses: `deploy/deploy.sh` (Task 1), `dist/fa-portal-v2.apk` (Task 3)

- [ ] **Step 1: Write the NPM script (idempotent; backs up first)**

`deploy/npm_add_mobile2.js`:
```js
// Adds /mobile2 (v2 dashboard, port 8091) to NPM proxy host 1, next to /mobile (v1). Safe to run twice.
// NPM regenerates 1.conf from its database, so both the DB row and 1.conf are updated.
const fs = require('fs');
const knex = require('knex')({client: 'sqlite3', connection: {filename: '/data/database.sqlite'}, useNullAsDefault: true});
(async () => {
  fs.copyFileSync('/data/database.sqlite', '/data/database.sqlite.bak-mobile2');
  const host = await knex('proxy_host').where({id: 1}).first();
  const locations = JSON.parse(host.locations || '[]');
  if (!locations.some((l) => l.path === '/mobile2')) {
    const v1 = locations.find((l) => l.path === '/mobile');
    locations.push({...v1, path: '/mobile2', forward_port: 8091,
      advanced_config: v1.advanced_config.replace('rewrite ^/mobile/?(.*)$', 'rewrite ^/mobile2/?(.*)$')});
    await knex('proxy_host').where({id: 1}).update({locations: JSON.stringify(locations)});
  }
  const conf = '/data/nginx/proxy_host/1.conf';
  let text = fs.readFileSync(conf, 'utf8');
  if (!text.includes('location /mobile2 {')) {
    fs.copyFileSync(conf, conf + '.bak-mobile2');
    const start = text.indexOf('  location /mobile {');
    const end = text.indexOf('\n  }\n', start) + 4;
    const block = text.slice(start, end)
      .replace('location /mobile {', 'location /mobile2 {')
      .replace('rewrite ^/mobile/?(.*)$', 'rewrite ^/mobile2/?(.*)$')
      .replace('http://192.168.100.44:8090;', 'http://192.168.100.44:8091;');
    text = text.slice(0, end) + '\n' + block + text.slice(end);
    fs.writeFileSync(conf, text);
  }
  process.exit(0);
})();
```
(nginx picks the longest matching prefix, so `/mobile2/...` goes to 8091 and `/mobile/...` still goes to 8090.)

- [ ] **Step 2: Deploy the dashboard and bundle the v2 APK**

```bash
cd ~/claude_projects/mobile-rpa-v2 && cp ~/claude_projects/fa-portal-v2/dist/fa-portal-v2.apk mobile_rpa/fa-portal-v2.apk && ./deploy/deploy.sh
```
Expected: `active` and `http 200` (served on 8091).

- [ ] **Step 3: Add the NPM location**

```bash
cd ~/claude_projects/mobile-rpa-v2 && scp -q -i ~/.ssh/id_root deploy/npm_add_mobile2.js root@192.168.100.20:/tmp/ && \
ssh -i ~/.ssh/id_root root@192.168.100.20 "pct push 110 /tmp/npm_add_mobile2.js /tmp/npm_add_mobile2.js && pct exec 110 -- bash -c '
  docker cp /tmp/npm_add_mobile2.js nginx-proxy-manager-app-1:/tmp/npm_add_mobile2.js &&
  docker exec nginx-proxy-manager-app-1 sh -c \"NODE_PATH=/app/node_modules node /tmp/npm_add_mobile2.js && nginx -t && nginx -s reload\"'"
for p in mobile mobile2; do rtk proxy curl -s -o /dev/null -w "$p %{http_code}\n" https://digimate.fastautomate.com/$p/; done
rtk proxy curl -s -o /dev/null -w "apk %{http_code} %{size_download}\n" https://digimate.fastautomate.com/mobile2/app/FastAutomate-v2.apk
```
Expected: `nginx: configuration file ... test is successful`, `mobile 200`, `mobile2 200`, `apk 200` with about 53 MB.

- [ ] **Step 4: Install v2 on the redroid next to v1 and turn its service on**

```bash
S=192.168.100.23:5555
adb -s $S install -r -g ~/claude_projects/fa-portal-v2/dist/fa-portal-v2.apk
adb -s $S shell appops set com.fastautomate.agent MANAGE_EXTERNAL_STORAGE allow
CUR=$(adb -s $S shell settings get secure enabled_accessibility_services | tr -d '\r')
adb -s $S shell settings put secure enabled_accessibility_services "$CUR:com.fastautomate.agent/com.mobilerun.portal.service.MobilerunAccessibilityService"
adb -s $S shell settings put secure accessibility_enabled 1
adb -s $S shell pm list packages | grep -E "com.mobilerun.portal|com.fastautomate.agent"
```
Expected: both packages listed. The redroid is a test device; v1's service stays on too.

- [ ] **Step 5: Connect the v2 app with an invite (the same path a user takes)**

```bash
C=/tmp/claude-1000/-home-arrow/573c1627-48ac-4d09-93fa-91d5ddaa417a/scratchpad/cj2
rtk proxy curl -s -c $C -H 'content-type: application/json' -d '{"password":"connect"}' https://digimate.fastautomate.com/mobile2/api/login
URL=$(rtk proxy curl -s -b $C -X POST https://digimate.fastautomate.com/mobile2/api/app/invite | python3 -c "import json,sys; print(json.load(sys.stdin)['url'])")
TOKEN=${URL#*t=}
adb -s 192.168.100.23:5555 shell am start -a android.intent.action.VIEW \
  -d "'fastautomate2://connect?token=$TOKEN&url=wss%3A%2F%2Fdigimate.fastautomate.com%2Fmobile2%2Fv1%2Fproviders%2Fpersonal%2Fjoin'" com.fastautomate.agent
sleep 8
rtk proxy curl -s -b $C https://digimate.fastautomate.com/mobile2/api/state | python3 -c "import json,sys; [print(p['name'], p['status']) for p in json.load(sys.stdin)['phones']]"
rtk proxy curl -s -o /dev/null -w "v1 %{http_code}\n" https://digimate.fastautomate.com/mobile/
```
Expected: one phone (`redroid12_x86_64 online`) on v2, and `v1 200`.

- [ ] **Step 6: Commit**

```bash
cd ~/claude_projects/mobile-rpa-v2 && git add deploy/npm_add_mobile2.js && git commit -q -m "deploy: NPM /mobile2 location for v2"
```

---

# Phase 2: the agent runs on the phone

Dashboard tasks (5 to 11) come first, then the app (12 to 18), then the end-to-end check (19). Run dashboard commands in `~/claude_projects/mobile-rpa-v2`; `pytest` means `.venv/bin/python -m pytest`.

### Task 5: `KeyManager` (OpenRouter key-management calls)

**Files:**
- Create: `mobile_rpa/openrouter_keys.py`
- Test: `tests/test_openrouter_keys.py`

**Interfaces:**
- Produces: `API = "https://openrouter.ai/api/v1/keys"`; `class KeyApiError(RuntimeError)`; `class KeyManager(management_key: str, transport: httpx.AsyncBaseTransport | None = None)` with:
  - `async create(name: str, daily_cap: float) -> tuple[str, str]` returning `(key, hash)`
  - `async delete(key_hash: str) -> None` (a 404 counts as done)
  - `async set_disabled(key_hash: str, disabled: bool) -> None`
  - `async set_limit(key_hash: str, daily_cap: float) -> None`
  - `async spent_today(key_hash: str) -> float`
- Live shapes (checked 2026-09-25):
  - `POST /keys` -> `{"data": {"hash", "limit", "limit_reset", "usage_daily", ...}, "key": "sk-or-v1-..."}`
  - `GET` and `PATCH /keys/{hash}` -> `{"data": {...}}`; `PATCH` accepts both `disabled` and `limit`
  - `DELETE` -> `{"deleted": true}`

- [ ] **Step 1: Write the failing tests**

`tests/test_openrouter_keys.py`:
```python
import json

import httpx
import pytest

from mobile_rpa.openrouter_keys import API, KeyApiError, KeyManager


def recording(log: list):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        log.append((request.method, str(request.url), body, request.headers["authorization"]))
        if request.method == "POST":
            return httpx.Response(201, json={"data": {"hash": "h1", "limit": 2.0}, "key": "sk-or-v1-phone"})
        if request.method == "GET":
            return httpx.Response(200, json={"data": {"hash": "h1", "usage_daily": 0.4321}})
        if request.method == "PATCH":
            return httpx.Response(200, json={"data": {"hash": "h1", **body}})
        return httpx.Response(404, json={"error": {"message": "not found"}})
    return httpx.MockTransport(handler)


async def test_create_key_with_a_daily_cap():
    log: list = []
    keys = KeyManager("mgmt", recording(log))
    assert await keys.create("fastautomate-v2-3-POCO F3", 2.0) == ("sk-or-v1-phone", "h1")
    method, url, body, auth = log[0]
    assert (method, url, auth) == ("POST", API, "Bearer mgmt")
    assert body == {"name": "fastautomate-v2-3-POCO F3", "limit": 2.0, "limit_reset": "daily"}


async def test_spend_pause_cap_and_delete():
    log: list = []
    keys = KeyManager("mgmt", recording(log))
    assert await keys.spent_today("h1") == 0.4321
    await keys.set_disabled("h1", True)
    await keys.set_limit("h1", 1.5)
    await keys.delete("h1")  # OpenRouter says 404: already gone, which is fine
    assert [(m, u.rsplit("/", 1)[1], b) for m, u, b, _ in log] == [
        ("GET", "h1", None), ("PATCH", "h1", {"disabled": True}), ("PATCH", "h1", {"limit": 1.5}), ("DELETE", "h1", None),
    ]


async def test_errors_are_readable():
    refuse = httpx.MockTransport(lambda r: httpx.Response(401, json={"error": {"message": "Invalid management key"}}))
    with pytest.raises(KeyApiError, match="401: Invalid management key"):
        await KeyManager("bad", refuse).create("x", 1.0)
    with pytest.raises(KeyApiError, match="management key"):
        KeyManager("")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_openrouter_keys.py -q`
Expected: FAIL (`No module named 'mobile_rpa.openrouter_keys'`).

- [ ] **Step 3: Write `mobile_rpa/openrouter_keys.py`**

```python
"""OpenRouter key management: the dashboard gives every phone its own capped key.

Needs the account's management key (openrouter.ai/settings/management-keys). Shapes checked live
2026-09-25: POST /keys -> {"data": {"hash", "limit", "usage_daily", ...}, "key": "sk-or-v1-..."};
GET and PATCH /keys/{hash} -> {"data": {...}} (PATCH takes "disabled" and "limit");
DELETE /keys/{hash} -> {"deleted": true}.
"""

from __future__ import annotations

import httpx

API = "https://openrouter.ai/api/v1/keys"


class KeyApiError(RuntimeError):
    pass


class KeyManager:
    def __init__(self, management_key: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not management_key:
            raise KeyApiError("Add the OpenRouter management key in Settings.")
        self._headers = {"Authorization": f"Bearer {management_key}"}
        self._transport = transport

    async def _call(self, method: str, url: str, body: dict | None = None) -> dict:
        try:
            async with httpx.AsyncClient(timeout=20, transport=self._transport) as client:
                resp = await client.request(method, url, json=body, headers=self._headers)
        except httpx.HTTPError as exc:
            raise KeyApiError(f"Could not reach OpenRouter: {type(exc).__name__}") from exc
        if resp.status_code == 404 and method == "DELETE":
            return {"deleted": True}
        if resp.status_code >= 400:
            raise KeyApiError(f"OpenRouter said {resp.status_code}: {_message(resp)}")
        return resp.json()

    async def create(self, name: str, daily_cap: float) -> tuple[str, str]:
        data = await self._call("POST", API, {"name": name[:60], "limit": daily_cap, "limit_reset": "daily"})
        return data["key"], data["data"]["hash"]

    async def delete(self, key_hash: str) -> None:
        await self._call("DELETE", f"{API}/{key_hash}")

    async def set_disabled(self, key_hash: str, disabled: bool) -> None:
        await self._call("PATCH", f"{API}/{key_hash}", {"disabled": disabled})

    async def set_limit(self, key_hash: str, daily_cap: float) -> None:
        await self._call("PATCH", f"{API}/{key_hash}", {"limit": daily_cap})

    async def spent_today(self, key_hash: str) -> float:
        data = await self._call("GET", f"{API}/{key_hash}")
        return round(float(data["data"].get("usage_daily") or 0), 4)


def _message(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("error", {}).get("message", ""))[:200]
    except ValueError:
        return resp.text[:200]
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/test_openrouter_keys.py -q`
Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add mobile_rpa/openrouter_keys.py tests/test_openrouter_keys.py && git commit -q -m "OpenRouter key management client (per-phone capped keys)"
```

### Task 6: Database fields for phone-run reports and phone keys

**Files:**
- Modify: `mobile_rpa/db.py` (`SCHEMA`, `Db.__init__`, `create_task`, `add_event`, `update_phone`; new `run_by_uuid`)
- Test: `tests/test_db_v2.py`

**Interfaces:**
- Produces:
  - `Db.create_task(prompt, reasoning, max_steps, runs, origin="dashboard") -> int`: each run dict may carry `"uuid"`, otherwise a new `uuid4().hex` is given
  - `Db.run_by_uuid(run_uuid: str) -> dict | None`
  - `Db.add_event(run_id, kind, text, seq: int | None = None, ts: str | None = None) -> dict | None`: `None` when `(run_id, seq)` is already stored
  - `Db.update_phone(..., key_hash=..., paused=...)` works
  - rows gain `runs.uuid`, `runs.origin`, `phones.key_hash`, `phones.paused`, `run_events.seq`

- [ ] **Step 1: Write the failing tests**

`tests/test_db_v2.py`:
```python
import sqlite3

from mobile_rpa.db import Db

RUN = {"phone_id": 1, "serial": "app:x", "phone_name": "P", "instruction": "open settings"}


def test_runs_get_a_uuid_and_events_are_unique_per_seq(tmp_path):
    db = Db(tmp_path / "t.db")
    run = db.task(db.create_task("open settings", True, 30, [RUN]))["runs"][0]
    assert len(run["uuid"]) == 32 and run["origin"] == "dashboard"
    assert db.run_by_uuid(run["uuid"])["id"] == run["id"]
    assert db.add_event(run["id"], "action", "Tap Settings", seq=1)["text"] == "Tap Settings"
    assert db.add_event(run["id"], "action", "Tap Settings", seq=1) is None  # resent after a reconnect
    assert db.add_event(run["id"], "phase", "local note") is not None  # dashboard notes have no seq
    assert [e["text"] for e in db.events(run["id"])] == ["Tap Settings", "local note"]


def test_app_started_runs_keep_the_phone_uuid(tmp_path):
    db = Db(tmp_path / "t.db")
    run = db.task(db.create_task("x", False, 10, [{**RUN, "uuid": "u-1"}], origin="app"))["runs"][0]
    assert (run["uuid"], run["origin"]) == ("u-1", "app")


def test_a_phase1_database_is_upgraded(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE phones (id INTEGER PRIMARY KEY, serial TEXT NOT NULL UNIQUE, name TEXT NOT NULL DEFAULT '',"
        " model TEXT NOT NULL DEFAULT '', android TEXT NOT NULL DEFAULT '', added_at TEXT NOT NULL, last_seen TEXT);"
        "CREATE TABLE runs (id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL, phone_id INTEGER NOT NULL,"
        " serial TEXT NOT NULL, phone_name TEXT NOT NULL, instruction TEXT NOT NULL, status TEXT NOT NULL,"
        " started_at TEXT, ended_at TEXT, steps INTEGER NOT NULL DEFAULT 0, result TEXT NOT NULL DEFAULT '');"
        "CREATE TABLE run_events (id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, ts TEXT NOT NULL,"
        " kind TEXT NOT NULL, text TEXT NOT NULL);"
    )
    conn.close()
    db = Db(path)
    phone = db.add_phone("app:z", "Z", "Z", "14")
    db.update_phone(phone["id"], key_hash="h", paused=1)
    assert (db.phone(phone["id"])["key_hash"], db.phone(phone["id"])["paused"]) == ("h", 1)
    run = db.task(db.create_task("x", True, 5, [RUN]))["runs"][0]
    assert db.add_event(run["id"], "ok", "done", seq=3) and db.add_event(run["id"], "ok", "done", seq=3) is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_db_v2.py -q`
Expected: FAIL (`KeyError: 'uuid'`).

- [ ] **Step 3: Change `db.py`**

Add `import uuid` at the top. In `SCHEMA`, give `phones` two more columns before `)`: `key_hash TEXT NOT NULL DEFAULT '', paused INTEGER NOT NULL DEFAULT 0`; give `runs` `uuid TEXT, origin TEXT NOT NULL DEFAULT 'dashboard'`; give `run_events` `seq INTEGER`. Below `SCHEMA` add:
```python
# columns added after the first v2 release (phase 1 used the v1 schema)
MIGRATIONS = (
    ("phones", "key_hash", "TEXT NOT NULL DEFAULT ''"),
    ("phones", "paused", "INTEGER NOT NULL DEFAULT 0"),
    ("runs", "uuid", "TEXT"),
    ("runs", "origin", "TEXT NOT NULL DEFAULT 'dashboard'"),
    ("run_events", "seq", "INTEGER"),
)
INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS runs_uuid ON runs(uuid);
CREATE UNIQUE INDEX IF NOT EXISTS run_events_seq ON run_events(run_id, seq);
"""
```
In `Db.__init__`, replace the `quality` block with:
```python
            for table, column, definition in MIGRATIONS:
                cols = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
                if column not in cols:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            self._conn.executescript(INDEXES)
```
Replace `create_task`, `add_event` and the `allowed` set of `update_phone`, and add `run_by_uuid` after `run`:
```python
    def create_task(
        self, prompt: str, reasoning: bool, max_steps: int, runs: list[dict], origin: str = "dashboard"
    ) -> int:
        task_id = self._run(
            "INSERT INTO tasks(prompt, created_at, reasoning, max_steps) VALUES(?, ?, ?, ?)",
            prompt, now(), int(reasoning), max_steps,
        )
        for run in runs:
            self._run(
                "INSERT INTO runs(task_id, phone_id, serial, phone_name, instruction, status, uuid, origin) "
                "VALUES(?, ?, ?, ?, ?, 'queued', ?, ?)",
                task_id, run["phone_id"], run["serial"], run["phone_name"], run["instruction"],
                run.get("uuid") or uuid.uuid4().hex, origin,
            )
        return task_id

    def run_by_uuid(self, run_uuid: str) -> dict | None:
        return self._one("SELECT * FROM runs WHERE uuid = ?", run_uuid)

    def add_event(
        self, run_id: int, kind: str, text: str, seq: int | None = None, ts: str | None = None
    ) -> dict | None:
        """None when this (run, seq) is already stored: a phone resending after a reconnect."""
        ts = ts or now()
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO run_events(run_id, ts, kind, text, seq) VALUES(?, ?, ?, ?, ?)",
                (run_id, ts, kind, text, seq),
            )
            if cur.rowcount == 0:
                return None
            event_id = cur.lastrowid
        return {"id": event_id, "run_id": run_id, "ts": ts, "kind": kind, "text": text, "seq": seq}
```
and `allowed = {"name", "model", "android", "last_seen", "serial", "key_hash", "paused"}`.

- [ ] **Step 4: Run all tests**

Run: `pytest -q`
Expected: all pass (the v1-era runner still calls `add_event(run_id, kind, text)`, which still returns a dict).

- [ ] **Step 5: Commit**

```bash
git add mobile_rpa/db.py tests/test_db_v2.py && git commit -q -m "db: run uuids, report sequence numbers (stored once), phone key hash and pause flag"
```

### Task 7: What the phone's agent is told (`agent_prompts.py`)

**Files:**
- Create: `mobile_rpa/agent_prompts.py`
- Test: `tests/test_agent_prompts.py`

**Interfaces:**
- Produces:
  - constants: `PROMPTS_VERSION: str`, `OPENROUTER = "https://openrouter.ai/api/v1"`, `PLANNER_SYSTEM: str`, `EXECUTOR_SYSTEM: str`, `TOOLS: list[dict]` (OpenAI function-tool schemas), `TOOL_NAMES = ("tap", "tap_at", "type", "scroll", "back", "home", "enter", "open_app", "wait", "done")`
  - `run_payload(task: dict, run: dict, settings: dict[str, str]) -> dict` with keys `uuid, instruction, reasoning, max_steps, time_limit_s, vision, planner_model, executor_model, base_url, prompts{version, planner, executor, tools}`
  - `defaults_json() -> str`, the same payload without `uuid`/`instruction`, using `RUNTIME_DEFAULTS` (bundled in the app in phase 3)
- Tool arguments (the app implements exactly these):
  - `tap{index:int}`, `tap_at{x:int, y:int}`, `type{text:str, index?:int}`
  - `scroll{direction: up|down|left|right, index?:int}`
  - `back{}`, `home{}`, `enter{}`, `open_app{name:str}`, `wait{seconds:number}`
  - `done{success:bool, answer:str}`

- [ ] **Step 1: Write the failing tests**

`tests/test_agent_prompts.py`:
```python
import json

from mobile_rpa.agent_prompts import OPENROUTER, TOOL_NAMES, TOOLS, defaults_json, run_payload

TASK = {"reasoning": 1, "max_steps": 12}
RUN = {"uuid": "u1", "instruction": "open settings"}
SETTINGS = {"planner_model": "p/m", "executor_model": "e/m", "timeout_minutes": "15", "vision": "0"}


def test_tools_are_the_ones_the_app_implements():
    assert tuple(t["function"]["name"] for t in TOOLS) == TOOL_NAMES
    for tool in TOOLS:
        params = tool["function"]["parameters"]
        assert tool["type"] == "function" and params["type"] == "object"
        assert set(params["required"]) <= set(params["properties"])
    scroll = next(t for t in TOOLS if t["function"]["name"] == "scroll")["function"]["parameters"]
    assert scroll["properties"]["direction"]["enum"] == ["up", "down", "left", "right"]


def test_run_payload_carries_everything_the_phone_needs():
    payload = run_payload(TASK, RUN, SETTINGS)
    assert payload["uuid"] == "u1" and payload["instruction"] == "open settings"
    assert (payload["reasoning"], payload["max_steps"], payload["time_limit_s"], payload["vision"]) == (True, 12, 900, False)
    assert (payload["planner_model"], payload["executor_model"], payload["base_url"]) == ("p/m", "e/m", OPENROUTER)
    assert payload["prompts"]["tools"] == TOOLS and payload["prompts"]["planner"] and payload["prompts"]["executor"]
    json.dumps(payload)  # goes over the wire


def test_defaults_for_the_app_have_no_task():
    defaults = json.loads(defaults_json())
    assert "uuid" not in defaults and "instruction" not in defaults
    assert defaults["max_steps"] == 30 and defaults["vision"] is True and defaults["prompts"]["tools"] == TOOLS
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_agent_prompts.py -q`
Expected: FAIL (module missing).

- [ ] **Step 3: Write `mobile_rpa/agent_prompts.py`**

```python
"""What the agent on each phone is told. Sent with every task (and bundled in the app for its own
runs), so behaviour can change without an app update. Tool names and arguments must match the
app's agent/Actions.kt."""

from __future__ import annotations

import json

from .settings import RUNTIME_DEFAULTS

PROMPTS_VERSION = "2026-09-25.1"
OPENROUTER = "https://openrouter.ai/api/v1"

PLANNER_SYSTEM = """You plan work for an AI agent that operates an Android phone by tapping, typing and scrolling.
Given the task and the current screen, write the shortest list of concrete goals (at most 6) that completes the task.
Each goal is one visible outcome, for example "Settings app is open" or "Search results for 'weather' are shown".
If the agent got stuck, choose a different route than the one that failed.
Reply with JSON only: {"goals": ["...", "..."]}"""

EXECUTOR_SYSTEM = """You operate an Android phone to complete a task. Each turn you get the task, the plan (when there is
one), your recent actions with their results, and the current screen as a numbered list of elements (sometimes also a
screenshot). Choose exactly ONE action by calling one tool.

Rules:
- Prefer tapping elements by their number. Use tap_at only when what you need is not in the list.
- To type, give the number of the text field so it gets focused first. Use enter to submit a search.
- If an action did not change the screen, try something different; never repeat it more than twice.
- To reach something off screen, scroll. To leave a screen, use back.
- Call done(success=true, answer=...) as soon as the task is complete, and put anything the task asked you to find in answer.
- Call done(success=false, answer=<why>) when the task is impossible, for example a login you have no credentials for.
- Never type passwords, payment details or personal data unless the task gives them to you."""


def _tool(name: str, description: str, properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": list(required)},
        },
    }


_INDEX = {"type": "integer", "description": "Element number from the screen list"}

TOOLS = [
    _tool("tap", "Tap a screen element by its number.", {"index": _INDEX}, ("index",)),
    _tool("tap_at", "Tap a point on the screen, in screen pixels.",
          {"x": {"type": "integer"}, "y": {"type": "integer"}}, ("x", "y")),
    _tool("type", "Type text into a field. Give the field's number to focus it first. Existing text is replaced.",
          {"text": {"type": "string"}, "index": _INDEX}, ("text",)),
    _tool("scroll", "Scroll the screen, or the numbered scrollable element, to reveal more. 'down' shows what is below.",
          {"direction": {"type": "string", "enum": ["up", "down", "left", "right"]}, "index": _INDEX}, ("direction",)),
    _tool("back", "Press the Back button.", {}),
    _tool("home", "Go to the home screen.", {}),
    _tool("enter", "Press Enter / Search on the keyboard.", {}),
    _tool("open_app", "Open an installed app by the name shown under its icon.", {"name": {"type": "string"}}, ("name",)),
    _tool("wait", "Wait for the screen to finish loading.",
          {"seconds": {"type": "number", "description": "1 to 10"}}, ("seconds",)),
    _tool("done", "Finish the task.",
          {"success": {"type": "boolean"}, "answer": {"type": "string", "description": "Result or reason"}},
          ("success", "answer")),
]
TOOL_NAMES = tuple(t["function"]["name"] for t in TOOLS)


def _common(reasoning: bool, max_steps: int, settings: dict[str, str]) -> dict:
    return {
        "reasoning": bool(reasoning),
        "max_steps": int(max_steps),
        "time_limit_s": max(1, int(settings.get("timeout_minutes") or 15)) * 60,
        "vision": settings.get("vision", "1") != "0",
        "planner_model": settings.get("planner_model") or RUNTIME_DEFAULTS["planner_model"],
        "executor_model": settings.get("executor_model") or RUNTIME_DEFAULTS["executor_model"],
        "base_url": OPENROUTER,
        "prompts": {"version": PROMPTS_VERSION, "planner": PLANNER_SYSTEM, "executor": EXECUTOR_SYSTEM, "tools": TOOLS},
    }


def run_payload(task: dict, run: dict, settings: dict[str, str]) -> dict:
    """The agent/run message for one phone."""
    return {"uuid": run["uuid"], "instruction": run["instruction"],
            **_common(bool(task["reasoning"]), int(task["max_steps"]), settings)}


def defaults_json() -> str:
    """Settings + instructions the app uses for its own runs before the dashboard has sent any."""
    d = RUNTIME_DEFAULTS
    return json.dumps(_common(d["reasoning"] != "0", int(d["max_steps"]), d), indent=1)
```
(`RUNTIME_DEFAULTS` gets its v2 content in Task 10; the fields used here, `planner_model`, `executor_model`, `max_steps`, `reasoning`, `vision` and `timeout_minutes`, already exist in v1.)

- [ ] **Step 4: Run the tests**

Run: `pytest tests/test_agent_prompts.py -q`
Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add mobile_rpa/agent_prompts.py tests/test_agent_prompts.py && git commit -q -m "agent instructions and tool schemas sent to the phones"
```

### Task 8: `Orchestrator` (tasks run on the phones; their reports are stored once and acked)

**Files:**
- Create: `mobile_rpa/orchestrator.py`
- Modify: `mobile_rpa/devices.py` (add `DeviceConn.notify`)
- Test: `tests/test_orchestrator.py`

**Interfaces:**
- Consumes:
  - `Db.create_task(..., origin=)`, `Db.run_by_uuid`, `Db.add_event(..., seq=, ts=)` (Task 6)
  - `run_payload(task, run, settings)` (Task 7)
  - `DeviceHub.get(device_id) -> DeviceConn`, `DeviceConn.call(method, params, timeout)`
- Produces:
  - `DeviceConn.notify(method: str, params: dict | None = None) -> None` (async; sends without waiting for an answer)
  - `class Orchestrator(db, broker, phones, devices, shots_dir: Path)` with:
    - `start_task(task_id: int) -> None`
    - `async stop_run(run_id: int) -> bool`
    - `async on_phone_message(device_id: str, message: dict) -> None`
    - `resume() -> None` (after a dashboard restart)
    - `close() -> None`
    - attribute `on_finish: Callable[[dict], Awaitable[None]] | None`
  - Broker events published: `task`, `run`, `run_event` (the same shapes v1's runner published)
  - Shots are saved as `shots_dir / f"{run_id}.jpg"`

- [ ] **Step 1: Write the failing tests**

`tests/test_orchestrator.py`:
```python
import asyncio
import base64

import pytest

from mobile_rpa.db import Db
from mobile_rpa.devices import DeviceError, app_serial
from mobile_rpa.events import Broker
from mobile_rpa.orchestrator import Orchestrator


class FakeConn:
    def __init__(self):
        self.calls, self.notes, self.refuse = [], [], None

    async def call(self, method, params=None, timeout=30):
        self.calls.append((method, params))
        if self.refuse:
            raise DeviceError(self.refuse)
        return {"accepted": True}

    async def notify(self, method, params=None):
        self.notes.append((method, params))


class FakeDevices:
    def __init__(self, conn):
        self.conn = conn

    def get(self, device_id):
        if self.conn is None:
            raise DeviceError("The phone is not connected")
        return self.conn


class FakePhones:
    def __init__(self):
        self.busy = {}

    def publish(self):
        pass


@pytest.fixture
def world(tmp_path):
    db = Db(tmp_path / "t.db")
    phone = db.add_phone(app_serial("dev-1"), "POCO F3", "POCO F3", "16")
    conn = FakeConn()
    orch = Orchestrator(db, Broker(), FakePhones(), FakeDevices(conn), tmp_path / "shots")
    return db, orch, conn, phone


def new_task(db, phone, text="open settings"):
    spec = {"phone_id": phone["id"], "serial": phone["serial"], "phone_name": phone["name"], "instruction": text}
    return db.create_task(text, True, 30, [spec])


async def started(db, orch, phone, text="open settings"):
    task_id = new_task(db, phone, text)
    orch.start_task(task_id)
    for _ in range(10):
        await asyncio.sleep(0)
    return db.task(task_id)["runs"][0]


def event(run, seq, kind="action", text="Tap Settings", **extra):
    return {"method": "agent/event", "params": {"uuid": run["uuid"], "seq": seq, "kind": kind, "text": text, **extra}}


async def test_task_goes_to_the_phone_and_reports_come_back(world):
    db, orch, conn, phone = world
    run = await started(db, orch, phone)
    method, payload = conn.calls[0]
    assert method == "agent/run" and payload["uuid"] == run["uuid"] and payload["instruction"] == "open settings"
    assert run["status"] == "running" and orch.phones.busy[phone["serial"]] == run["id"]
    await orch.on_phone_message("dev-1", event(run, 1, steps=1))
    shot = base64.b64encode(b"\xff\xd8jpeg").decode()
    done = {"uuid": run["uuid"], "seq": 2, "status": "succeeded", "result": "Settings is open", "steps": 1, "shot": shot}
    await orch.on_phone_message("dev-1", {"method": "agent/finished", "params": done})
    run = db.run(run["id"])
    assert (run["status"], run["result"], run["steps"]) == ("succeeded", "Settings is open", 1)
    assert (orch.shots_dir / f"{run['id']}.jpg").read_bytes() == b"\xff\xd8jpeg"
    assert conn.notes == [("agent/ack", {"uuid": run["uuid"], "seq": 1}), ("agent/ack", {"uuid": run["uuid"], "seq": 2})]
    assert phone["serial"] not in orch.phones.busy
    assert [e["kind"] for e in db.events(run["id"])][-2:] == ["action", "done"]


async def test_duplicate_events_are_stored_once(world):
    db, orch, conn, phone = world
    run = await started(db, orch, phone)
    await orch.on_phone_message("dev-1", event(run, 1))
    await orch.on_phone_message("dev-1", event(run, 1))  # resent after a reconnect
    assert [e["text"] for e in db.events(run["id"]) if e["kind"] == "action"] == ["Tap Settings"]
    assert len(conn.notes) == 2  # both copies acked


async def test_unknown_run_events_are_acked_and_dropped(world):
    db, orch, conn, _ = world
    await orch.on_phone_message("dev-1", {"method": "agent/event", "params": {"uuid": "nope", "seq": 4, "kind": "ok", "text": "x"}})
    assert conn.notes == [("agent/ack", {"uuid": "nope", "seq": 4})]


async def test_app_started_runs_appear_on_the_dashboard(world):
    db, orch, conn, phone = world
    start = {"uuid": "u-9", "seq": 0, "instruction": "check the weather", "reasoning": False, "max_steps": 12}
    await orch.on_phone_message("dev-1", {"method": "agent/started", "params": start})
    await orch.on_phone_message("dev-1", {"method": "agent/started", "params": start})  # resent
    run = db.run_by_uuid("u-9")
    task = db.task(run["task_id"])
    assert (task["prompt"], task["reasoning"], task["max_steps"]) == ("check the weather", 0, 12)
    assert (run["origin"], run["status"], len(db.tasks())) == ("app", "running", 1)
    assert orch.phones.busy[phone["serial"]] == run["id"]


async def test_a_phone_that_refuses_fails_the_run(world):
    db, orch, conn, phone = world
    conn.refuse = "Turn on FastAutomate v2 in Accessibility"
    run = await started(db, orch, phone)
    run = db.run(run["id"])
    assert run["status"] == "failed" and "Accessibility" in run["result"]
    assert phone["serial"] not in orch.phones.busy


async def test_stop_asks_the_phone_and_offline_phones_stop_here(world):
    db, orch, conn, phone = world
    run = await started(db, orch, phone)
    assert await orch.stop_run(run["id"])
    assert conn.calls[-1] == ("agent/stop", {"uuid": run["uuid"]})
    orch.devices.conn = None  # the phone went offline
    assert await orch.stop_run(run["id"])
    assert db.run(run["id"])["status"] == "stopped"
    await orch.on_phone_message("dev-1", event(run, 7))  # a late report after that is ignored
    assert not [e for e in db.events(run["id"]) if e["seq"] == 7]


async def test_silent_phone_fails_after_the_time_limit(world, monkeypatch):
    db, orch, conn, phone = world
    monkeypatch.setattr(Orchestrator, "_deadline_s", lambda self, settings: 0.05)
    run = await started(db, orch, phone)
    await asyncio.sleep(0.2)
    run = db.run(run["id"])
    assert run["status"] == "failed" and "stopped reporting" in run["result"]


async def test_resume_after_a_dashboard_restart(world, monkeypatch):
    db, orch, conn, phone = world
    monkeypatch.setattr(Orchestrator, "_deadline_s", lambda self, settings: 30)
    run = await started(db, orch, phone)
    fresh = Orchestrator(db, Broker(), FakePhones(), FakeDevices(conn), orch.shots_dir)
    fresh.resume()
    assert fresh.phones.busy[phone["serial"]] == run["id"] and run["id"] in fresh._watchdogs
    fresh.close()
    orch.close()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_orchestrator.py -q`
Expected: FAIL (module missing).

- [ ] **Step 3: Add `notify` to `DeviceConn`** (in `mobile_rpa/devices.py`, after `call`)

```python
    async def notify(self, method: str, params: dict | None = None) -> None:
        """A message the phone does not answer, such as agent/ack."""
        async with self._send_lock:
            await self.ws.send_text(json.dumps({"method": method, "params": params or {}}))
```

- [ ] **Step 4: Write `mobile_rpa/orchestrator.py`**

```python
"""Tasks run on the phones themselves. The dashboard sends ``agent/run``; the phone's FastAutomate v2
app does every step and reports with ``agent/started`` (its own runs), ``agent/event`` and
``agent/finished``. Every report is acked with ``agent/ack`` so the phone can drop it from its
outbox; a report that arrives twice (after a reconnect) is stored once."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from .agent_prompts import run_payload
from .db import Db, now
from .devices import DeviceError, DeviceHub, app_serial, device_id_of
from .events import Broker

log = logging.getLogger("mobile_rpa.orchestrator")

ACTIVE = ("queued", "running")
FINAL = ("succeeded", "failed", "stopped")
SILENCE_GRACE_S = 300  # after the time limit, how long a silent phone gets before its run is failed


class Orchestrator:
    def __init__(self, db: Db, broker: Broker, phones, devices: DeviceHub, shots_dir: Path) -> None:
        self.db, self.broker, self.phones, self.devices = db, broker, phones, devices
        self.shots_dir = shots_dir
        self.shots_dir.mkdir(parents=True, exist_ok=True)
        self.on_finish: Callable[[dict], Awaitable[None]] | None = None
        self._watchdogs: dict[int, asyncio.Task] = {}
        self._jobs: set[asyncio.Task] = set()

    # ---- dashboard -> phone ----------------------------------------------------------------
    def start_task(self, task_id: int) -> None:
        task = self.db.task(task_id)
        if not task:
            return
        for run in task["runs"]:
            if run["status"] == "queued":
                self.phones.busy[run["serial"]] = run["id"]
                self._spawn(self._send_run(task, run))
        self.phones.publish()

    async def _send_run(self, task: dict, run: dict) -> None:
        settings = self.db.settings()
        self._update(run, status="running", started_at=now())
        self._event(run, "phase", "Sending the task to the phone")
        try:
            conn = self.devices.get(device_id_of(run["serial"]) or "")
            await conn.call("agent/run", run_payload(task, run, settings), timeout=20)
        except (DeviceError, TimeoutError) as exc:
            self._finish(run, "failed", f"The phone did not take the task: {exc or 'no answer'}")
            return
        self._watch(run["id"], self._deadline_s(settings))

    async def stop_run(self, run_id: int) -> bool:
        run = self.db.run(run_id)
        if not run or run["status"] not in ACTIVE:
            return False
        try:
            conn = self.devices.get(device_id_of(run["serial"]) or "")
            await conn.call("agent/stop", {"uuid": run["uuid"]}, timeout=10)
        except (DeviceError, TimeoutError):  # offline: close it here, ignore its late reports
            self._finish(run, "stopped", "Stopped by operator (the phone was not reachable).")
        return True

    # ---- phone -> dashboard ----------------------------------------------------------------
    async def on_phone_message(self, device_id: str, message: dict) -> None:
        method, params = message.get("method"), message.get("params") or {}
        if method not in ("agent/started", "agent/event", "agent/finished"):
            return
        run_uuid, seq = str(params.get("uuid") or ""), params.get("seq")
        try:
            if method == "agent/started":
                self._phone_started(device_id, params)
                return
            run = self.db.run_by_uuid(run_uuid)
            if run is None:
                log.warning("phone %s reported on unknown run %s; dropped", device_id, run_uuid)
            elif method == "agent/event":
                self._phone_event(run, params)
            else:
                self._phone_finished(run, params)
        finally:  # always ack, so a report the dashboard cannot use never clogs the phone's outbox
            with contextlib.suppress(Exception):
                await self.devices.get(device_id).notify("agent/ack", {"uuid": run_uuid, "seq": seq})

    def _phone_started(self, device_id: str, params: dict) -> None:
        run_uuid = str(params.get("uuid") or "")
        if not run_uuid or self.db.run_by_uuid(run_uuid):
            return  # resent after a reconnect
        phone = self.db.phone_by_serial(app_serial(device_id))
        if not phone:
            return
        instruction = str(params.get("instruction") or "").strip()[:4000] or "(no instruction)"
        spec = {"phone_id": phone["id"], "serial": phone["serial"], "phone_name": phone["name"],
                "instruction": instruction, "uuid": run_uuid}
        task_id = self.db.create_task(instruction, bool(params.get("reasoning", True)),
                                      int(params.get("max_steps") or 30), [spec], origin="app")
        run = self.db.task(task_id)["runs"][0]
        self.phones.busy[phone["serial"]] = run["id"]
        self.db.update_run(run["id"], status="running", started_at=str(params.get("ts") or now()))
        self.broker.publish("task", self.db.task(task_id))
        self.phones.publish()
        self._watch(run["id"], self._deadline_s(self.db.settings()))

    def _phone_event(self, run: dict, params: dict) -> None:
        if run["status"] not in ACTIVE:
            return  # the run already ended here (stopped while offline): late reports are ignored
        event = self.db.add_event(run["id"], str(params.get("kind") or "status")[:20],
                                  str(params.get("text") or "")[:4000], seq=params.get("seq"), ts=params.get("ts"))
        if event is None:
            return  # already stored
        if params.get("steps") is not None:
            self.db.update_run(run["id"], steps=int(params["steps"]))
        self.broker.publish("run_event", {**event, "task_id": run["task_id"], "steps": params.get("steps")})

    def _phone_finished(self, run: dict, params: dict) -> None:
        if run["status"] not in ACTIVE:
            return
        status = params.get("status") if params.get("status") in FINAL else "failed"
        if params.get("shot"):
            with contextlib.suppress(ValueError, binascii.Error):
                (self.shots_dir / f"{run['id']}.jpg").write_bytes(base64.b64decode(params["shot"]))
        extra = {"steps": int(params["steps"])} if params.get("steps") is not None else {}
        self._finish(run, status, str(params.get("result") or "")[:4000], **extra)

    # ---- lifecycle ----------------------------------------------------------------------
    def resume(self) -> None:
        """After a dashboard restart: runs keep going on the phones, so watch them again."""
        settings = self.db.settings()
        for run in self.db.active_runs():
            self.phones.busy[run["serial"]] = run["id"]
            self._watch(run["id"], self._deadline_s(settings))

    def close(self) -> None:
        for dog in self._watchdogs.values():
            dog.cancel()
        self._watchdogs.clear()

    # ---- helpers -----------------------------------------------------------------------
    def _deadline_s(self, settings: dict[str, str]) -> float:
        return max(1, int(settings.get("timeout_minutes") or 15)) * 60 + SILENCE_GRACE_S

    def _watch(self, run_id: int, seconds: float) -> None:
        async def dog() -> None:
            await asyncio.sleep(seconds)
            self._watchdogs.pop(run_id, None)
            run = self.db.run(run_id)
            if run and run["status"] in ACTIVE:
                self._finish(run, "failed", "The phone stopped reporting (no result within the time limit).")

        old = self._watchdogs.pop(run_id, None)
        if old:
            old.cancel()
        self._watchdogs[run_id] = asyncio.get_running_loop().create_task(dog())

    def _finish(self, run: dict, status: str, result: str, **fields) -> None:
        dog = self._watchdogs.pop(run["id"], None)
        if dog and dog is not asyncio.current_task():
            dog.cancel()
        self._update(run, status=status, ended_at=now(), result=result, **fields)
        self._event(run, "done" if status == "succeeded" else "fail", result or status)
        self.phones.busy.pop(run["serial"], None)
        self.phones.publish()
        if self.on_finish:
            self._spawn(self.on_finish(self.db.run(run["id"]) or run))

    def _update(self, run: dict, **fields) -> None:
        self.db.update_run(run["id"], **fields)
        self.broker.publish("run", {**(self.db.run(run["id"]) or {}), "task_id": run["task_id"]})

    def _event(self, run: dict, kind: str, text: str) -> None:
        event = self.db.add_event(run["id"], kind, text)
        if event:
            self.broker.publish("run_event", {**event, "task_id": run["task_id"], "steps": None})

    def _spawn(self, coro) -> None:
        job = asyncio.get_running_loop().create_task(coro)
        self._jobs.add(job)
        job.add_done_callback(self._jobs.discard)
```
Note: `resume()` calls `_watch()`, which needs a running loop. It is called from the FastAPI lifespan (Task 10) and from an async test, so a loop exists.

- [ ] **Step 5: Run the tests**

Run: `pytest tests/test_orchestrator.py -q`
Expected: `8 passed`.

- [ ] **Step 6: Commit**

```bash
git add mobile_rpa/orchestrator.py mobile_rpa/devices.py tests/test_orchestrator.py && git commit -q -m "orchestrator: tasks run on the phones, reports stored once and acked, watchdog for silent phones"
```

### Task 9: `PhoneKeys` (one capped key per phone)

**Files:**
- Create: `mobile_rpa/phone_keys.py`
- Modify: `mobile_rpa/settings.py` (two new runtime settings; Task 10 later replaces the file with the same keys)
- Test: `tests/test_phone_keys.py`

**Interfaces:**
- Consumes: `KeyManager`, `KeyApiError` (Task 5); `Db.phone`, `Db.update_phone(key_hash=, paused=)` (Task 6); `DeviceConn.call`
- Produces: `class PhoneKeys(db: Db, managers: Callable[[str], KeyManager] = KeyManager)` with:
  - `async ensure(phone_id: int, conn, presented_hash: str) -> str | None`: returns a problem for the phone card, or `None`
  - `async forget(phone: dict) -> None`
  - `async pause(phone: dict, paused: bool) -> None`
  - `async apply_cap(daily_cap: float) -> None`
  - `async spent_today(phone: dict) -> float | None`
  - `NAME_PREFIX = "fastautomate-v2-"`
- The phone gets its key through `agent/credentials {key, hash, base_url}`.

- [ ] **Step 1: Write the failing tests**

`tests/test_phone_keys.py`:
```python
from mobile_rpa.db import Db
from mobile_rpa.devices import DeviceError, app_serial
from mobile_rpa.openrouter_keys import KeyApiError
from mobile_rpa.phone_keys import PhoneKeys


class FakeManager:
    def __init__(self, log):
        self.log, self.n = log, 0

    async def create(self, name, daily_cap):
        self.n += 1
        self.log.append(("create", name, daily_cap))
        return f"sk-or-v1-key{len(self.log)}", f"hash{len(self.log)}"

    async def delete(self, key_hash):
        self.log.append(("delete", key_hash))

    async def set_disabled(self, key_hash, disabled):
        self.log.append(("disabled", key_hash, disabled))

    async def set_limit(self, key_hash, daily_cap):
        self.log.append(("limit", key_hash, daily_cap))

    async def spent_today(self, key_hash):
        return 0.25


class FakeConn:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def call(self, method, params=None, timeout=30):
        self.calls.append((method, params))
        if self.fail:
            raise DeviceError("gone")
        return {"saved": True}


def setup(tmp_path, management_key="mgmt"):
    db = Db(tmp_path / "t.db")
    db.set_settings({"management_key": management_key, "daily_cap_usd": "1.50"})
    phone = db.add_phone(app_serial("d1"), "POCO F3", "POCO F3", "16")
    log: list = []

    def managers(key):
        if not key:
            raise KeyApiError("Add the OpenRouter management key in Settings.")
        return FakeManager(log)

    return db, phone, log, PhoneKeys(db, managers)


async def test_a_new_phone_gets_its_own_capped_key(tmp_path):
    db, phone, log, keys = setup(tmp_path)
    conn = FakeConn()
    assert await keys.ensure(phone["id"], conn, "") is None
    assert log == [("create", f"fastautomate-v2-{phone['id']}-POCO F3", 1.5)]
    assert conn.calls == [("agent/credentials", {"key": "sk-or-v1-key1", "hash": "hash1", "base_url": "https://openrouter.ai/api/v1"})]
    assert db.phone(phone["id"])["key_hash"] == "hash1"


async def test_a_phone_that_still_has_its_key_is_left_alone(tmp_path):
    db, phone, log, keys = setup(tmp_path)
    db.update_phone(phone["id"], key_hash="hash-old")
    conn = FakeConn()
    assert await keys.ensure(phone["id"], conn, "hash-old") is None
    assert log == [] and conn.calls == []


async def test_a_reinstalled_app_gets_a_fresh_key_and_the_old_one_is_deleted(tmp_path):
    db, phone, log, keys = setup(tmp_path)
    db.update_phone(phone["id"], key_hash="hash-old")
    assert await keys.ensure(phone["id"], FakeConn(), "") is None
    assert log[0] == ("delete", "hash-old") and log[1][0] == "create"


async def test_no_management_key_is_a_readable_problem(tmp_path):
    db, phone, log, keys = setup(tmp_path, management_key="")
    conn = FakeConn()
    assert "management key" in await keys.ensure(phone["id"], conn, "")
    assert conn.calls == []


async def test_a_paused_phone_gets_a_disabled_key(tmp_path):
    db, phone, log, keys = setup(tmp_path)
    db.update_phone(phone["id"], paused=1)
    await keys.ensure(phone["id"], FakeConn(), "")
    assert log[-1] == ("disabled", "hash1", True)


async def test_forget_pause_cap_and_spend(tmp_path):
    db, phone, log, keys = setup(tmp_path)
    db.update_phone(phone["id"], key_hash="h9")
    phone = db.phone(phone["id"])
    await keys.pause(phone, True)
    assert db.phone(phone["id"])["paused"] == 1
    await keys.apply_cap(3.0)
    assert await keys.spent_today(phone) == 0.25
    await keys.forget(phone)
    assert log == [("disabled", "h9", True), ("limit", "h9", 3.0), ("delete", "h9")]


async def test_phone_that_drops_before_receiving_its_key_reports_it(tmp_path):
    db, phone, log, keys = setup(tmp_path)
    assert "Could not give the phone its AI key" in await keys.ensure(phone["id"], FakeConn(fail=True), "")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_phone_keys.py -q`
Expected: FAIL (module missing).

- [ ] **Step 3: Add the two settings** (the DB drops settings it does not know)

In `mobile_rpa/settings.py`, add to `RUNTIME_DEFAULTS` the entries `"management_key": "",` and `"daily_cap_usd": "2.00",`, and change `SECRET_SETTINGS = {"api_key"}` to `SECRET_SETTINGS = {"api_key", "management_key"}`.

- [ ] **Step 4: Write `mobile_rpa/phone_keys.py`**

```python
"""One OpenRouter key per phone, capped per day, so every phone pays for its own AI and can be
paused alone. The key goes to the phone once (agent/credentials) and is never stored here; the
dashboard keeps only its hash. On every join the app says which key it holds (X-Agent-Key-Hash)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable

from .agent_prompts import OPENROUTER
from .db import Db
from .devices import DeviceError
from .openrouter_keys import KeyApiError, KeyManager

NAME_PREFIX = "fastautomate-v2-"


class PhoneKeys:
    def __init__(self, db: Db, managers: Callable[[str], KeyManager] = KeyManager) -> None:
        self.db = db
        self._managers = managers
        self._locks: dict[int, asyncio.Lock] = {}

    def _manager(self) -> KeyManager:
        return self._managers(self.db.settings().get("management_key", ""))

    def _cap(self) -> float:
        try:
            return float(self.db.settings().get("daily_cap_usd") or 2)
        except ValueError:
            return 2.0

    async def ensure(self, phone_id: int, conn, presented_hash: str) -> str | None:
        """Make sure the phone holds a working key. Returns a problem to show on its card, or None."""
        async with self._locks.setdefault(phone_id, asyncio.Lock()):
            phone = self.db.phone(phone_id)
            if not phone:
                return None
            if phone["key_hash"] and presented_hash == phone["key_hash"]:
                return None
            try:
                keys = self._manager()
                if phone["key_hash"]:
                    await keys.delete(phone["key_hash"])  # the app lost it (reinstall): replace it
                key, key_hash = await keys.create(f"{NAME_PREFIX}{phone_id}-{phone['name']}", self._cap())
                if phone["paused"]:
                    await keys.set_disabled(key_hash, True)
            except KeyApiError as exc:
                return str(exc)
            self.db.update_phone(phone_id, key_hash=key_hash)
            try:
                await conn.call("agent/credentials", {"key": key, "hash": key_hash, "base_url": OPENROUTER}, timeout=20)
            except (DeviceError, TimeoutError) as exc:
                return f"Could not give the phone its AI key: {exc or 'no answer'}"
            return None

    async def forget(self, phone: dict) -> None:
        if phone.get("key_hash"):
            with contextlib.suppress(KeyApiError):
                await self._manager().delete(phone["key_hash"])

    async def pause(self, phone: dict, paused: bool) -> None:
        if phone.get("key_hash"):
            await self._manager().set_disabled(phone["key_hash"], paused)
        self.db.update_phone(phone["id"], paused=int(paused))

    async def apply_cap(self, daily_cap: float) -> None:
        keys = self._manager()
        for phone in self.db.phones():
            if phone["key_hash"]:
                with contextlib.suppress(KeyApiError):
                    await keys.set_limit(phone["key_hash"], daily_cap)

    async def spent_today(self, phone: dict) -> float | None:
        if not phone.get("key_hash"):
            return None
        try:
            return await self._manager().spent_today(phone["key_hash"])
        except KeyApiError:
            return None
```

- [ ] **Step 5: Run the tests**

Run: `pytest -q`
Expected: all pass (`tests/test_phone_keys.py`: 7).

- [ ] **Step 6: Commit**

```bash
git add mobile_rpa/phone_keys.py tests/test_phone_keys.py && git commit -q -m "per-phone keys: create on join, replace after a reinstall, pause, cap, spend, forget"
```

### Task 10: Wire the v2 dashboard (settings, phones, join handshake, orchestrator) and remove the server-side agent and adb

**Files:**
- Replace: `mobile_rpa/settings.py`, `mobile_rpa/phones.py`, `mobile_rpa/app.py`
- Modify: `mobile_rpa/devices.py` (`DeviceHub.on_message`, `serve(on_ready=)`), `mobile_rpa/appconnect.py` (join handshake, `register` signature, relay removed), `mobile_rpa/splitter.py` (OpenAI format only), `pyproject.toml` (dependencies), `deploy/mobile-rpa-v2.service` (drop `MRPA_ADB`)
- Delete: `mobile_rpa/adb.py`, `mobile_rpa/scrcpy.py`, `mobile_rpa/scrcpy-server-v4.1.jar`, `mobile_rpa/pairing.py`, `mobile_rpa/runner.py`, `mobile_rpa/worker.py`, `tests/test_scrcpy.py`, `tests/test_pairing.py`, `tests/test_worker.py`, `tests/fake_worker.py`
- Replace: `tests/test_app.py`; modify `tests/test_appconnect.py`, `tests/test_splitter.py`

**Interfaces:**
- Consumes: `Orchestrator` (Task 8), `PhoneKeys` (Task 9), `KeyManager` (Task 5)
- Produces:
  - `create_app(env: Env | None = None) -> FastAPI` (no `adb` argument)
  - `app.state.db`, `app.state.phones`, `app.state.orchestrator`, `app.state.phone_keys`, `app.state.devices`
  - `GET /api/runs/{run_id}/shot.jpg`
  - `Env(password, data_dir, session_secret, public_url=...)`
  - `RUNTIME_DEFAULTS` keys: `api_key, management_key, daily_cap_usd, planner_model, executor_model, max_steps, reasoning, vision, timeout_minutes`; `SECRET_SETTINGS = {"api_key", "management_key"}`
  - `DeviceHub.on_message: Callable[[str, dict], Awaitable[None]] | None`
  - `DeviceHub.serve(ws, device_id, name, on_ready: Callable[[DeviceConn], None] | None = None)`
  - `appconnect.register(app, *, env, db, phones, orchestrator, devices, start_task, on_phone_ready) -> None`
  - `PhoneRegistry(db, broker, devices)` with `set_note(serial, note)`

- [ ] **Step 1: Write the new `tests/test_app.py` (the failing tests)**

```python
import base64
import io
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from mobile_rpa import app as app_module
from mobile_rpa.appconnect import JOIN_PATH
from mobile_rpa.app import create_app
from mobile_rpa.settings import Env


@pytest.fixture
def client(tmp_path):
    env = Env(password="connect", data_dir=tmp_path, session_secret=b"k" * 32)
    with TestClient(create_app(env)) as c:
        yield c


def login(c):
    assert c.post("/api/login", json={"password": "connect"}).status_code == 200


def jpeg_b64() -> str:
    out = io.BytesIO()
    Image.new("RGB", (216, 480), (18, 144, 79)).save(out, "JPEG")
    return base64.b64encode(out.getvalue()).decode()


def wait_for(predicate, seconds: float = 5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


class FakeKeys:
    """Stands in for OpenRouter's key management."""
    def __init__(self):
        self.log = []

    async def create(self, name, daily_cap):
        self.log.append(("create", name, daily_cap))
        return "sk-or-v1-phone", f"hash-{len(self.log)}"

    async def delete(self, key_hash):
        self.log.append(("delete", key_hash))

    async def set_disabled(self, key_hash, disabled):
        self.log.append(("disabled", key_hash, disabled))

    async def set_limit(self, key_hash, cap):
        self.log.append(("limit", key_hash, cap))

    async def spent_today(self, key_hash):
        return 0.12


def ready(client, monkeypatch) -> FakeKeys:
    """Keys in Settings, credit on the account, OpenRouter faked."""
    fake = FakeKeys()
    client.app.state.phone_keys._managers = lambda key: fake
    async def credit(settings):
        return 5.0
    monkeypatch.setattr(app_module, "credit_left", credit)
    client.put("/api/settings", json={"api_key": "sk-or-v1-dash-1234", "management_key": "sk-or-v1-mgmt-9876"})
    return fake


class AgentPhone:
    """A FastAutomate v2 app: takes every task, reports one action, then the result."""
    def __init__(self, ws):
        self.ws, self.calls, self.stop = ws, [], threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self.stop.is_set():
            try:
                msg = self.ws.receive_json()
            except Exception:
                return
            self.calls.append(msg)
            if msg.get("id") is None:
                continue  # acks need no answer
            method, p = msg["method"], msg.get("params") or {}
            result = jpeg_b64() if method == "screenshot" else {"accepted": True}
            if method == "state":
                result = {"a11y_tree": {}, "device_context": {"screen_bounds": {"width": 1080, "height": 2400}}}
            self.ws.send_json({"id": msg["id"], "status": "success", "result": result})
            if method == "agent/run" and "hang" not in p["instruction"]:
                budget = "budget" in p["instruction"]
                status = "failed" if budget or "fail" in p["instruction"] else "succeeded"
                result = "This phone's daily AI budget is used up" if budget else "did " + p["instruction"]
                self.ws.send_json({"method": "agent/event", "params": {"uuid": p["uuid"], "seq": 1, "kind": "action", "text": "Tap Settings", "steps": 1}})
                self.ws.send_json({"method": "agent/finished", "params": {"uuid": p["uuid"], "seq": 2, "status": status, "result": result, "steps": 1, "shot": jpeg_b64()}})
            if method == "agent/stop":
                self.ws.send_json({"method": "agent/finished", "params": {"uuid": p["uuid"], "seq": 9, "status": "stopped", "result": "Stopped on the phone", "steps": 0}})

    def sent(self, method):
        return [m for m in self.calls if m.get("method") == method]


def join(client, device_id="dev-1", key_hash=""):
    token = client.post("/api/app/invite").json()["url"].split("t=")[1]
    headers = {"Authorization": f"Bearer {token}", "X-Device-ID": device_id, "X-Device-Name": "POCO F3", "X-Android-Version": "16"}
    if key_hash:
        headers["X-Agent-Key-Hash"] = key_hash
    return client.websocket_connect(JOIN_PATH, headers=headers)


def the_phone(client):
    return wait_for(lambda: next(iter(client.get("/api/state").json()["phones"]), None))


def keyed_phone(client):
    phone = the_phone(client)
    wait_for(lambda: client.app.state.db.phone(phone["id"])["key_hash"])
    return phone


def run_task(client, phone, instruction="open settings"):
    r = client.post("/api/tasks", json={"prompt": instruction, "runs": [{"phone_id": phone["id"], "instruction": instruction}]})
    assert r.status_code == 200, r.text
    return r.json()


def test_api_requires_login(client):
    assert client.get("/api/state").status_code == 401
    assert client.post("/api/login", json={"password": "nope"}).status_code == 401
    login(client)
    assert client.get("/api/state").status_code == 200


def test_index_is_public_and_uncached(client):
    r = client.get("/")
    assert r.status_code == 200 and "Mobile RPA" in r.text and r.headers["cache-control"] == "no-store"


def test_settings_never_return_secrets(client):
    login(client)
    s = client.put("/api/settings", json={"api_key": "sk-or-v1-dash-1234", "management_key": "sk-or-v1-mgmt-9876", "daily_cap_usd": "1.50"}).json()
    assert "api_key" not in s and "management_key" not in s
    assert (s["api_key_hint"], s["management_key_hint"], s["daily_cap_usd"], s["ready"]) == ("...1234", "...9876", "1.50", True)
    assert client.put("/api/settings", json={"daily_cap_usd": "0"}).status_code == 400
    assert client.put("/api/settings", json={"daily_cap_usd": "abc"}).status_code == 400
    assert client.put("/api/settings", json={"provider": "anthropic"}).json().get("provider") is None


def test_phone_gets_its_own_key_when_it_joins(client, monkeypatch):
    login(client)
    fake = ready(client, monkeypatch)
    with join(client) as ws:
        phone = AgentPhone(ws)
        keyed_phone(client)
        creds = wait_for(lambda: phone.sent("agent/credentials"))[0]["params"]
        assert creds == {"key": "sk-or-v1-phone", "hash": "hash-1", "base_url": "https://openrouter.ai/api/v1"}
        assert fake.log == [("create", f"fastautomate-v2-{the_phone(client)['id']}-POCO F3", 2.0)]
        phone.stop.set()


def test_task_runs_on_the_phone_itself(client, monkeypatch):
    login(client)
    ready(client, monkeypatch)
    with join(client) as ws:
        phone = AgentPhone(ws)
        task = run_task(client, keyed_phone(client))
        run = wait_for(lambda: next((r for r in client.get(f"/api/tasks/{task['id']}").json()["runs"] if r["status"] == "succeeded"), None))
        assert (run["result"], run["steps"]) == ("did open settings", 1)
        sent = phone.sent("agent/run")[0]["params"]
        assert sent["uuid"] == run["uuid"] and sent["executor_model"] == "google/gemini-2.5-flash" and sent["prompts"]["tools"]
        assert "sk-or-v1-dash" not in json.dumps(sent) and "sk-or-v1-mgmt" not in json.dumps(sent)
        assert [e["kind"] for e in run["events"]][-2:] == ["action", "done"]
        assert client.get(f"/api/runs/{run['id']}/shot.jpg").content.startswith(b"\xff\xd8")
        wait_for(lambda: [m["params"]["seq"] for m in phone.sent("agent/ack")] == [1, 2])
        phone.stop.set()


def test_busy_phone_rejected_and_stop_asks_the_phone(client, monkeypatch):
    login(client)
    ready(client, monkeypatch)
    with join(client) as ws:
        phone = AgentPhone(ws)
        p = keyed_phone(client)
        task = run_task(client, p, "hang around")
        wait_for(lambda: phone.sent("agent/run"))
        again = client.post("/api/tasks", json={"prompt": "x", "runs": [{"phone_id": p["id"], "instruction": "x"}]})
        assert again.status_code == 409
        assert client.post(f"/api/tasks/{task['id']}/stop").json()["stopped"]
        wait_for(lambda: client.get(f"/api/tasks/{task['id']}").json()["runs"][0]["status"] == "stopped")
        phone.stop.set()


def test_phone_without_a_key_cannot_take_tasks(client, monkeypatch):
    login(client)
    async def credit(settings):
        return 5.0
    monkeypatch.setattr(app_module, "credit_left", credit)
    client.put("/api/settings", json={"api_key": "sk-or-v1-dash-1234"})  # no management key
    with join(client) as ws:
        phone = AgentPhone(ws)
        p = the_phone(client)
        r = client.post("/api/tasks", json={"prompt": "x", "runs": [{"phone_id": p["id"], "instruction": "x"}]})
        assert r.status_code == 409 and "AI key" in r.json()["detail"]
        assert "management key" in wait_for(lambda: the_phone(client)["note"])
        phone.stop.set()


def test_empty_credit_blocks_runs_with_a_clear_reason(client, monkeypatch):
    login(client)
    ready(client, monkeypatch)
    async def empty(settings):
        return 0.01
    monkeypatch.setattr(app_module, "credit_left", empty)
    with join(client) as ws:
        phone = AgentPhone(ws)
        p = keyed_phone(client)
        r = client.post("/api/tasks", json={"prompt": "x", "runs": [{"phone_id": p["id"], "instruction": "x"}]})
        assert r.status_code == 402 and "credit" in r.json()["detail"]
        phone.stop.set()


def test_removing_a_phone_deletes_its_key(client, monkeypatch):
    login(client)
    fake = ready(client, monkeypatch)
    with join(client) as ws:
        phone = AgentPhone(ws)
        p = keyed_phone(client)
        assert client.delete(f"/api/phones/{p['id']}").status_code == 200
        assert ("delete", "hash-1") in fake.log
        phone.stop.set()


def test_split_single_phone_is_verbatim(client, monkeypatch):
    login(client)
    ready(client, monkeypatch)
    with join(client) as ws:
        phone = AgentPhone(ws)
        p = the_phone(client)
        r = client.post("/api/split", json={"prompt": "Open Settings", "phone_ids": [p["id"]]}).json()
        assert r["instructions"][0]["instruction"] == "Open Settings"
        phone.stop.set()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_app.py -q`
Expected: FAIL (`Env.__init__() missing 1 required positional argument: 'adb'`, among others).

- [ ] **Step 3: Delete the server-side agent and adb**

```bash
git rm -q mobile_rpa/adb.py mobile_rpa/scrcpy.py mobile_rpa/scrcpy-server-v4.1.jar mobile_rpa/pairing.py \
  mobile_rpa/runner.py mobile_rpa/worker.py tests/test_scrcpy.py tests/test_pairing.py tests/test_worker.py tests/fake_worker.py
```

- [ ] **Step 4: Replace `mobile_rpa/settings.py`**

```python
"""Process configuration (env) and the runtime settings operators edit in the dashboard (v2)."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

# gemini-2.5-flash answers in ~1s vs ~9s for qwen3-vl-235b at the same price (measured 2026-09-25)
DEFAULT_MODEL = "google/gemini-2.5-flash"
OPENROUTER = "https://openrouter.ai/api/v1"

# Runtime settings: key -> default. Stored in the DB table `settings`, editable in the UI.
RUNTIME_DEFAULTS: dict[str, str] = {
    "api_key": "",  # the dashboard's own key: splitting a task across phones, account credit
    "management_key": "",  # creates one capped key per phone (openrouter.ai/settings/management-keys)
    "daily_cap_usd": "2.00",  # per phone, resets daily
    "planner_model": DEFAULT_MODEL,
    "executor_model": DEFAULT_MODEL,
    "max_steps": "30",
    "reasoning": "1",
    "vision": "1",
    "timeout_minutes": "15",
}
SECRET_SETTINGS = {"api_key", "management_key"}


@dataclass(frozen=True)
class Env:
    password: str
    data_dir: Path
    session_secret: bytes
    public_url: str = "https://digimate.fastautomate.com/mobile2"  # where phones reach the dashboard

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mobile-rpa.db"


def _session_secret(data_dir: Path) -> bytes:
    """A per-install secret persisted next to the DB, so sessions survive restarts."""
    path = data_dir / "session.key"
    if path.exists():
        return path.read_bytes()
    key = secrets.token_bytes(32)
    path.write_bytes(key)
    path.chmod(0o600)
    return key


def load_env() -> Env:
    data_dir = Path(os.environ.get("MRPA_DATA_DIR", "./data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    return Env(
        password=os.environ.get("MRPA_PASSWORD", "connect"),
        data_dir=data_dir,
        session_secret=_session_secret(data_dir),
        public_url=os.environ.get("MRPA_PUBLIC_URL", "https://digimate.fastautomate.com/mobile2"),
    )


def llm(settings: dict[str, str]) -> dict[str, str]:
    """The dashboard's own connection (OpenRouter, OpenAI format)."""
    return {"provider": "openai", "key": settings.get("api_key", ""), "base_url": OPENROUTER}
```
In `mobile_rpa/agent_prompts.py`, replace `OPENROUTER = "https://openrouter.ai/api/v1"` with `from .settings import OPENROUTER` (single source).

- [ ] **Step 5: `splitter.py`: OpenAI format only; credit through either key**

Replace the body of `chat()` from `conn = llm(settings)` through the `except (KeyError, IndexError, ValueError, TypeError)` block with:
```python
    conn = llm(settings)
    if not conn["key"]:
        raise SplitError("Add the OpenRouter API key in Settings first.")
    url = conn["base_url"] + "/chat/completions"
    headers = {"Authorization": f"Bearer {conn['key']}"}
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user}]
    body = {"model": settings["planner_model"], "max_tokens": max_tokens, "messages": messages}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise SplitError(f"Could not reach the model provider: {type(exc).__name__} {exc}".strip()) from exc
    if resp.status_code != 200:
        raise SplitError(f"Model provider said {resp.status_code}: {_error_text(resp)}")
    try:
        return (resp.json()["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        raise SplitError("Unexpected reply from the model provider.") from exc
```
Replace `credit_left` with:
```python
async def credit_left(settings: dict[str, str]) -> float | None:
    """Remaining OpenRouter account balance in USD (either key can read it), or None on any error."""
    key = settings.get("api_key") or settings.get("management_key")
    if not key:
        return None
    for attempt in range(3):  # a blip (DNS, TLS) must not hide the balance
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(OPENROUTER + "/credits", headers={"Authorization": f"Bearer {key}"})
            data = resp.json()["data"]
            return round(float(data["total_credits"]) - float(data["total_usage"]), 4)
        except httpx.HTTPError:
            await asyncio.sleep(1 + attempt)
        except (KeyError, ValueError, TypeError):
            return None
    return None
```
Change the import to `from .settings import OPENROUTER, llm`. In `tests/test_splitter.py`, delete `test_claude_chat_uses_messages_api`.

- [ ] **Step 6: `devices.py`: report hook and ready hook**

In `DeviceHub.__init__` add `self.on_message = None  # async (device_id, message) for agent/* reports`. Change `serve` to:
```python
    async def serve(self, ws: WebSocket, device_id: str, name: str, on_ready=None) -> None:
        """Run one phone's connection until it drops."""
        old = self.conns.get(device_id)
        if old is not None:  # the app reconnected: the new socket wins
            old.fail_all("replaced by a newer connection")
            with contextlib.suppress(Exception):
                await old.ws.close()
        conn = DeviceConn(ws, device_id, name)
        self.conns[device_id] = conn
        self._changed()
        if on_ready:
            on_ready(conn)
        try:
            while True:
                incoming = await ws.receive()
                if incoming.get("type") == "websocket.disconnect":
                    break
                message = None
                with contextlib.suppress(ValueError, TypeError):
                    message = json.loads(incoming.get("text") or "")
                if not isinstance(message, dict):
                    continue
                if "method" not in message:
                    conn.feed(message)
                elif self.on_message and str(message["method"]).startswith("agent/"):
                    try:
                        await self.on_message(device_id, message)
                    except Exception:
                        log.exception("phone %s: report %s failed", device_id, message.get("method"))
        except Exception as exc:  # WebSocketDisconnect and friends
            log.info("phone %s disconnected: %s", device_id, type(exc).__name__)
        finally:
            conn.fail_all("the phone disconnected")
            if self.conns.get(device_id) is conn:
                del self.conns[device_id]
            self._changed()
```

- [ ] **Step 7: `appconnect.py`: join handshake, no relay**

- Change the `register` signature to `def register(app: FastAPI, *, env, db, phones, orchestrator, devices: DeviceHub, start_task, on_phone_ready) -> None:`.
- Delete the `relay_key = ...` line, the whole `# ---- relay for the agent` section (the `@app.api_route("/v1/relay/...")` function), and the final `return relay_key`.
- Delete the now unused imports `hashlib`, `hmac` and `JSONResponse`.
- In the docstring, delete the `/v1/relay` bullet.
- In `join`, replace the last two lines with:
```python
        presented_hash = (ws.headers.get("x-agent-key-hash") or "").strip()[:128]
        await phones.add_app_phone(device_id, name, android)
        await devices.serve(ws, device_id, name, on_ready=lambda conn: on_phone_ready(device_id, conn, presented_hash))
```
- In `cancel` (`/v1/tasks/{task_id}/cancel`), replace `await runner.stop_run(run["id"])` with `await orchestrator.stop_run(run["id"])`.

- [ ] **Step 8: Replace `mobile_rpa/phones.py`**

```python
"""Saved phones (all connected through the FastAutomate v2 app), their state and thumbnails."""

from __future__ import annotations

import asyncio
import contextlib
import time

from .db import Db
from .devices import DeviceHub, app_serial, device_id_of
from .events import Broker

THUMB_MAX_AGE = 5.0  # cards ask every 10 s; several open dashboards share one capture


class PhoneRegistry:
    def __init__(self, db: Db, broker: Broker, devices: DeviceHub) -> None:
        self.db, self.broker, self.devices = db, broker, devices
        self.busy: dict[str, int] = {}  # serial -> run id
        self.notes: dict[str, str] = {}  # serial -> problem shown on the card
        self._thumbs: dict[str, tuple[float, bytes]] = {}
        self._thumb_locks: dict[str, asyncio.Lock] = {}

    def status(self, serial: str) -> str:
        if serial in self.busy:
            return "busy"
        return "online" if self.devices.connected(device_id_of(serial)) else "offline"

    def view(self, phone: dict) -> dict:
        serial = phone["serial"]
        return {**phone, "link": "app", "status": self.status(serial), "run_id": self.busy.get(serial),
                "note": self.notes.get(serial, "")}

    def all(self) -> list[dict]:
        return [self.view(p) for p in self.db.phones()]

    def publish(self) -> None:
        self.broker.publish("phones", self.all())

    def set_note(self, serial: str, note: str) -> None:
        if self.notes.get(serial, "") == note:
            return
        if note:
            self.notes[serial] = note[:200]
        else:
            self.notes.pop(serial, None)
        self.publish()

    async def add_app_phone(self, device_id: str, name: str, android: str = "") -> dict:
        """Name and Android version follow what the app reports on each connect."""
        name = (name or "Phone").strip()[:60]
        phone = self.db.add_phone(app_serial(device_id), name, name, android)
        changes = {k: v for k, v in (("name", name), ("model", name), ("android", android)) if v and phone.get(k) != v}
        if changes:
            self.db.update_phone(phone["id"], **changes)
            phone = {**phone, **changes}
        self.publish()
        return self.view(phone)

    async def remove(self, phone_id: int) -> None:
        phone = self.db.phone(phone_id)
        if not phone:
            return
        serial = phone["serial"]
        self.db.delete_phone(phone_id)
        self._thumbs.pop(serial, None)
        self.notes.pop(serial, None)
        device_id = device_id_of(serial) or ""
        self.db.revoke_device_tokens(device_id)  # the app cannot silently come back
        if self.devices.connected(device_id):
            with contextlib.suppress(Exception):
                await self.devices.get(device_id).ws.close()
        self.publish()

    async def rename(self, phone_id: int, name: str) -> None:
        self.db.update_phone(phone_id, name=name.strip()[:60])
        self.publish()

    async def thumbnail(self, serial: str) -> bytes:
        """A small accessibility screenshot (480 px JPEG), cached briefly."""
        lock = self._thumb_locks.setdefault(serial, asyncio.Lock())
        async with lock:
            cached = self._thumbs.get(serial)
            if cached and time.monotonic() - cached[0] < THUMB_MAX_AGE:
                return cached[1]
            jpeg = await self.devices.get(device_id_of(serial) or "").screenshot(480, 60)
            self._thumbs[serial] = (time.monotonic(), jpeg)
            return jpeg
```

- [ ] **Step 9: Replace `mobile_rpa/app.py`**

```python
"""FastAPI app: REST + SSE for the dashboard. v2: phones join through the FastAutomate v2 app and run
tasks themselves; this dashboard plans, assigns and records what they report."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import appconnect, auth
from .db import Db
from .devices import GLOBAL_ACTIONS, DeviceConn, DeviceError, DeviceHub, app_serial, device_id_of
from .events import Broker
from .orchestrator import Orchestrator
from .phone_keys import PhoneKeys
from .phones import PhoneRegistry
from .settings import RUNTIME_DEFAULTS, SECRET_SETTINGS, Env, load_env
from .splitter import SplitError, credit_left, split, test_connection

log = logging.getLogger("mobile_rpa")
STATIC = Path(__file__).with_name("static")
LOW_CREDIT = 0.05  # USD: below this a task would die on its first model calls
CREDIT_POLL_SECONDS = 120
MAX_PHONES_PER_TASK = 50


class LoginBody(BaseModel):
    password: str


class RenameBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class SplitBody(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    phone_ids: list[int] = Field(min_length=1, max_length=MAX_PHONES_PER_TASK)


class RunSpec(BaseModel):
    phone_id: int
    instruction: str = Field(min_length=1, max_length=4000)


class TaskBody(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    reasoning: bool = True
    max_steps: int = Field(default=30, ge=3, le=200)
    runs: list[RunSpec] = Field(min_length=1, max_length=MAX_PHONES_PER_TASK)


class KeyBody(BaseModel):
    key: str


def masked_settings(values: dict[str, str]) -> dict[str, str | bool]:
    out: dict[str, str | bool] = {k: v for k, v in values.items() if k not in SECRET_SETTINGS}
    for name in SECRET_SETTINGS:
        key = values.get(name, "")
        out[f"{name}_set"] = bool(key)
        out[f"{name}_hint"] = f"...{key[-4:]}" if len(key) >= 8 else ""
    out["ready"] = bool(values.get("api_key")) and bool(values.get("management_key"))
    return out


def create_app(env: Env | None = None) -> FastAPI:
    env = env or load_env()
    db = Db(env.db_path)
    broker = Broker()
    devices = DeviceHub()  # phones connected through the FastAutomate v2 app
    phones = PhoneRegistry(db, broker, devices)
    devices.on_change = phones.publish
    orchestrator = Orchestrator(db, broker, phones, devices, env.data_dir / "shots")
    devices.on_message = orchestrator.on_phone_message
    phone_keys = PhoneKeys(db)
    credit: dict = {"value": None}
    jobs: set[asyncio.Task] = set()

    def spawn(coro) -> None:
        job = asyncio.get_running_loop().create_task(coro)
        jobs.add(job)
        job.add_done_callback(jobs.discard)

    async def refresh_credit() -> float | None:
        value = await credit_left(db.settings())
        if value != credit["value"]:
            credit["value"] = value
            broker.publish("credit", {"credit": value, "low": _low(value)})
        return value

    async def after_run(run: dict) -> None:
        with contextlib.suppress(Exception):
            await refresh_credit()

    orchestrator.on_finish = after_run

    async def credit_loop() -> None:
        while True:
            with contextlib.suppress(Exception):
                await refresh_credit()
            await asyncio.sleep(CREDIT_POLL_SECONDS)

    async def give_key(device_id: str, conn: DeviceConn, presented_hash: str) -> None:
        phone = db.phone_by_serial(app_serial(device_id))
        if phone:
            problem = await phone_keys.ensure(phone["id"], conn, presented_hash)
            phones.set_note(phone["serial"], problem or "")

    def on_phone_ready(device_id: str, conn: DeviceConn, presented_hash: str) -> None:
        spawn(give_key(device_id, conn, presented_hash))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        orchestrator.resume()
        poller = asyncio.create_task(credit_loop())
        yield
        poller.cancel()
        orchestrator.close()

    app = FastAPI(title="Mobile RPA v2", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.db, app.state.phones, app.state.devices = db, phones, devices
    app.state.orchestrator, app.state.phone_keys = orchestrator, phone_keys

    def authed(request_cookies: dict) -> bool:
        return auth.valid(env.session_secret, request_cookies.get(auth.COOKIE))

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        public = path in ("/api/login", "/api/commerce/balance")  # balance: asked by the phone app
        if path.startswith("/api/") and not public and not authed(request.cookies):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    def phone_or_404(phone_id: int) -> dict:
        phone = db.phone(phone_id)
        if not phone:
            raise HTTPException(404, "No such phone")
        return phone

    # ---- auth ----------------------------------------------------------------------------
    @app.post("/api/login")
    async def login(body: LoginBody, request: Request):
        if not auth.check_password(body.password, env.password):
            await asyncio.sleep(0.6)  # slows guessing without a lockout to manage
            return JSONResponse({"error": "Wrong password"}, status_code=401)
        response = JSONResponse({"ok": True})
        response.set_cookie(
            auth.COOKIE, auth.issue(env.session_secret), max_age=auth.SESSION_SECONDS, httponly=True,
            samesite="lax", secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https",
        )
        return response

    @app.post("/api/logout")
    async def logout():
        response = JSONResponse({"ok": True})
        response.delete_cookie(auth.COOKIE)
        return response

    # ---- state + events ------------------------------------------------------------------
    @app.get("/api/state")
    async def state():
        active_task_ids = sorted({r["task_id"] for r in db.active_runs()}, reverse=True)
        return {
            "phones": phones.all(),
            "active_tasks": [db.task(t) for t in active_task_ids],
            "stats": db.stats_today(),
            "settings": masked_settings(db.settings()),
            "credit": {"credit": credit["value"], "low": _low(credit["value"])},
        }

    @app.post("/api/credit/refresh")
    async def credit_refresh():
        value = await refresh_credit()
        return {"credit": value, "low": _low(value)}

    @app.get("/api/events")
    async def events():
        return StreamingResponse(broker.stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---- phones --------------------------------------------------------------------------
    @app.patch("/api/phones/{phone_id}")
    async def rename_phone(phone_id: int, body: RenameBody):
        phone_or_404(phone_id)
        await phones.rename(phone_id, body.name)
        return {"ok": True}

    @app.delete("/api/phones/{phone_id}")
    async def remove_phone(phone_id: int):
        phone = phone_or_404(phone_id)
        if phone["serial"] in phones.busy:
            raise HTTPException(409, "Stop the task running on this phone first.")
        await phone_keys.forget(phone)
        await phones.remove(phone_id)
        return {"ok": True}

    @app.get("/api/phones/{phone_id}/screen.jpg")
    async def screen(phone_id: int):
        phone = phone_or_404(phone_id)
        try:
            jpeg = await phones.thumbnail(phone["serial"])
        except (DeviceError, TimeoutError) as exc:
            raise HTTPException(503, f"Screen not available: {exc}") from exc
        return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.post("/api/phones/{phone_id}/key")
    async def press_key(phone_id: int, body: KeyBody):
        phone = phone_or_404(phone_id)
        if body.key not in GLOBAL_ACTIONS:
            raise HTTPException(400, "Unknown key")
        try:
            await devices.get(device_id_of(phone["serial"]) or "").call("global", {"action": GLOBAL_ACTIONS[body.key]})
        except (DeviceError, TimeoutError) as exc:
            raise HTTPException(503, str(exc) or "The phone did not answer") from exc
        return {"ok": True}

    # ---- tasks ---------------------------------------------------------------------------
    def pick_phones(ids: list[int]) -> list[dict]:
        picked, seen = [], set()
        for phone_id in ids:
            if phone_id in seen:
                continue
            seen.add(phone_id)
            phone = phone_or_404(phone_id)
            status = phones.status(phone["serial"])
            if status != "online":
                raise HTTPException(409, f"{phone['name']} is {status}, pick an online phone.")
            picked.append(phone)
        return picked

    @app.post("/api/split")
    async def split_task(body: SplitBody):
        picked = pick_phones(body.phone_ids)
        try:
            lines = await split(body.prompt, [p["name"] for p in picked], db.settings())
        except SplitError as exc:
            raise HTTPException(502, str(exc)) from exc
        return {"instructions": [{"phone_id": p["id"], "name": p["name"], "instruction": line}
                                 for p, line in zip(picked, lines, strict=True)]}

    @app.post("/api/tasks")
    async def create_task(body: TaskBody):
        specs = [{"phone_id": r.phone_id, "instruction": r.instruction} for r in body.runs]
        return await launch(body.prompt, specs, body.reasoning, body.max_steps)

    async def launch(prompt: str, specs: list[dict], reasoning: bool | None = None, max_steps: int | None = None) -> dict:
        """Start a task on phones (dashboard, or a phone's own app): same checks for both."""
        values = db.settings()
        if _low(credit["value"]):
            value = await refresh_credit()  # maybe they just topped up
            if _low(value):
                raise HTTPException(402, f"The AI credit is empty (${value:.2f} left), so the task would stop on its "
                                         "first step. Add credit at openrouter.ai/settings/credits, then press Run again.")
        picked = {p["id"]: p for p in pick_phones([sp["phone_id"] for sp in specs])}
        for phone in picked.values():
            if not phone["key_hash"]:
                raise HTTPException(409, f"{phone['name']} has no AI key yet. Add the OpenRouter management key in "
                                         "Settings, then keep the phone connected for a moment.")
        runs = [{"phone_id": sp["phone_id"], "serial": picked[sp["phone_id"]]["serial"],
                 "phone_name": picked[sp["phone_id"]]["name"], "instruction": sp["instruction"].strip()}
                for sp in specs if sp["phone_id"] in picked]
        reasoning = values.get("reasoning") != "0" if reasoning is None else reasoning
        max_steps = max_steps or int(values.get("max_steps") or 30)
        task_id = db.create_task(prompt.strip(), reasoning, max_steps, runs)
        orchestrator.start_task(task_id)
        task = db.task(task_id)
        broker.publish("task", task)
        return task

    @app.get("/api/tasks")
    async def list_tasks(limit: int = 30, offset: int = 0):
        return db.tasks(min(max(limit, 1), 100), max(offset, 0))

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: int):
        task = db.task(task_id)
        if not task:
            raise HTTPException(404, "No such task")
        for run in task["runs"]:
            run["events"] = db.events(run["id"])
        return task

    @app.post("/api/tasks/{task_id}/stop")
    async def stop_task(task_id: int):
        task = db.task(task_id)
        if not task:
            raise HTTPException(404, "No such task")
        return {"stopped": [r["id"] for r in task["runs"] if await orchestrator.stop_run(r["id"])]}

    @app.post("/api/runs/{run_id}/stop")
    async def stop_run(run_id: int):
        if not await orchestrator.stop_run(run_id):
            raise HTTPException(409, "That run is not running.")
        return {"ok": True}

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: int):
        return db.events(run_id)

    @app.get("/api/runs/{run_id}/shot.jpg")
    async def run_shot(run_id: int):
        path = env.data_dir / "shots" / f"{run_id}.jpg"
        if not path.exists():
            raise HTTPException(404, "No final screenshot for this run")
        return FileResponse(path, media_type="image/jpeg")

    # ---- settings ------------------------------------------------------------------------
    @app.get("/api/settings")
    async def get_settings():
        return masked_settings(db.settings())

    @app.put("/api/settings")
    async def put_settings(body: dict):
        clean: dict[str, str] = {}
        for key, value in body.items():
            if key not in RUNTIME_DEFAULTS or value is None:
                continue
            text = str(value).strip()
            if key in SECRET_SETTINGS and not text:
                continue  # blank means "keep the current key"
            if key in ("max_steps", "timeout_minutes") and not text.isdigit():
                raise HTTPException(400, f"{key} must be a whole number")
            if key == "daily_cap_usd":
                try:
                    cap = float(text)
                except ValueError:
                    raise HTTPException(400, "The daily cap must be a number of dollars") from None
                if not 0.05 <= cap <= 100:
                    raise HTTPException(400, "The daily cap must be between $0.05 and $100")
                text = f"{cap:.2f}"
            clean[key] = text[:500]
        before = db.settings()
        db.set_settings(clean)
        if clean.keys() & {"api_key", "management_key"}:
            spawn(refresh_credit())
        if clean.get("management_key") and clean["management_key"] != before.get("management_key"):
            for phone in db.phones():  # connected phones get their keys now
                device_id = device_id_of(phone["serial"]) or ""
                if not phone["key_hash"] and devices.connected(device_id):
                    spawn(give_key(device_id, devices.get(device_id), ""))
        if "daily_cap_usd" in clean and clean["daily_cap_usd"] != before.get("daily_cap_usd"):
            spawn(phone_keys.apply_cap(float(clean["daily_cap_usd"])))
        values = masked_settings(db.settings())
        broker.publish("settings", values)
        return values

    @app.post("/api/settings/test")
    async def test_settings():
        values = db.settings()
        credit_value = await credit_left(values)
        try:
            reply = await test_connection(values)
        except SplitError as exc:
            return {"ok": False, "error": str(exc), "credit": credit_value}
        return {"ok": True, "reply": reply, "credit": credit_value}

    # ---- live view (silent accessibility screenshots) --------------------------------------
    @app.websocket("/ws/phones/{phone_id}")
    async def live(ws: WebSocket, phone_id: int):
        if not authed(ws.cookies):
            await ws.close(code=4401)
            return
        phone = db.phone(phone_id)
        if not phone:
            await ws.close(code=4404)
            return
        await ws.accept()
        try:
            await appconnect.live_view(ws, devices.get(device_id_of(phone["serial"]) or ""))
        except (WebSocketDisconnect, RuntimeError, json.JSONDecodeError):
            pass
        except (DeviceError, TimeoutError) as exc:
            with contextlib.suppress(Exception):
                await ws.send_text(json.dumps({"type": "error", "error": f"Live view failed: {exc}"}))
                await ws.close()

    # Cloudflare caches .js/.css by URL, so every deploy gets new asset URLs.
    version = hashlib.sha1(b"".join((STATIC / name).read_bytes() for name in ("app.js", "app.css"))).hexdigest()[:10]
    index_html = ((STATIC / "index.html").read_text()
                  .replace('href="app.css"', f'href="app.css?v={version}"')
                  .replace('src="app.js"', f'src="app.js?v={version}"'))

    @app.get("/")
    async def index():
        return HTMLResponse(index_html, headers={"Cache-Control": "no-store"})

    @app.get("/api/qr")
    async def qr_svg(text: str):
        if not text or len(text) > 500:
            raise HTTPException(400, "Nothing to encode")
        return {"svg": _qr_svg(text)}

    @app.get("/api/commerce/balance")
    async def app_balance():
        raise HTTPException(404, "Credits are managed on the dashboard")

    appconnect.register(app, env=env, db=db, phones=phones, orchestrator=orchestrator, devices=devices,
                        start_task=launch, on_phone_ready=on_phone_ready)
    app.mount("/", StaticFiles(directory=STATIC), name="static")
    return app


def _qr_svg(text: str) -> str:
    import qrcode
    import qrcode.image.svg

    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode()


def _low(value: float | None) -> bool:
    return value is not None and value < LOW_CREDIT
```

- [ ] **Step 10: Dependencies, unit file, and the copied app-connect tests**

- `pyproject.toml`: in `dependencies`, delete `"mobilerun[anthropic]==0.6.19",` and `"av>=12",`; change package-data to `mobile_rpa = ["static/*", "*.apk"]`. Then run `uv pip install -q -p .venv -e '.[dev]'`.
- `deploy/mobile-rpa-v2.service`: delete the `Environment=MRPA_ADB=/usr/local/bin/adb` line.
- `tests/test_appconnect.py`:
  - In `test_app_prompt_runs_a_task_on_its_own_phone`, replace `client.put("/api/settings", json={"api_key": "sk-test-key-0000"})` with `from .test_app import ready; ready(client, monkeypatch)`, add `monkeypatch` to the test's arguments, and right after the phone connects add `wait_for(lambda: client.app.state.db.phone(app_phone(client)["id"])["key_hash"])`. The fake phone answers `agent/credentials` like any other call.
  - Delete any import of `mobile_rpa.adb` or `mobile_rpa.scrcpy` left in the file.

- [ ] **Step 11: Run all tests**

Run: `pytest -q`
Expected: all pass. `grep -rn "adb\|scrcpy\|runner\|worker" mobile_rpa/*.py` prints nothing except comments that say adb is gone.

- [ ] **Step 12: Commit**

```bash
git add -A && git commit -q -m "v2 dashboard: tasks run on the phones (orchestrator), per-phone keys on join, OpenRouter-only settings; server-side agent and adb removed"
```

### Task 11: Dashboard v2 screens (settings, tiles without video, final screenshots, quiet thumbnails)

**Files:**
- Modify: `mobile_rpa/static/app.js` (`openSettings`, `saveSettings`, `FORMAT_PRESETS`, `updateTile`, `renderBoard`, `clearFinished`, `dropTile`, `syncTileStreams`, `thumbTick`, `openTask`, `boot`)
- Test: `tests/test_static.py`

**Interfaces:**
- Consumes: `GET api/runs/{id}/shot.jpg` and the settings `management_key`, `daily_cap_usd` (Task 10).

- [ ] **Step 1: Write the failing test** (append to `tests/test_static.py`)

```python
def test_v2_screens():
    js = (STATIC / "app.js").read_text()
    assert "new LiveView(tile" not in js and "syncTileStreams" not in js  # no video on task tiles
    assert "shot.jpg" in js  # final screenshot on tiles and in history
    for field in ("management_key", "daily_cap_usd"):
        assert field in js
    assert "s-format" not in js and "FORMAT_PRESETS" not in js  # OpenRouter only
    assert "10000" in js  # card thumbnails every 10 s
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/test_static.py -q`
Expected: FAIL on `new LiveView(tile`.

- [ ] **Step 3: Task tiles: no live video; final screenshot when finished**

- Delete `const tileViews = new Map(); // run id -> LiveView`, the functions `dropTile` and `syncTileStreams`, the line `document.addEventListener('visibilitychange', syncTileStreams);`, and the `syncTileStreams();` call at the end of `renderBoard`.
- In `route()`, change `if (page === 'tasks') { renderChips(); syncTileStreams(); }` to `if (page === 'tasks') renderChips();` and delete the line `if (page !== 'tasks') syncTileStreams();`. Then `grep -n "tileViews\|dropTile\|syncTileStreams" mobile_rpa/static/app.js` must print nothing.
- In `renderBoard`, change the dismiss handler to `g.querySelector('.dismiss').onclick = () => { S.tasks.delete(t.id); renderBoard(); };`.
- In `clearFinished`, change the loop body to `if (!t.runs.some(isActive)) S.tasks.delete(id);`.
- At the end of `updateTile` add:
```js
  const scr = tile.querySelector('.screen');
  const want = isActive(r) ? 'working' : 'shot';
  if (scr.dataset.mode !== want) {
    scr.dataset.mode = want;
    scr.innerHTML = want === 'working'
      ? '<div class="ov"><span class="spin"></span> Working on the phone</div>'
      : `<img alt="Final screen" src="api/runs/${r.id}/shot.jpg" onerror="this.replaceWith(Object.assign(document.createElement('div'), {className: 'ov', textContent: 'No final screenshot'}))">`;
  }
```

- [ ] **Step 4: Phone cards: thumbnails every 10 s, none while the phone is working**

In `thumbTick`, change the skip condition to `if (!img || img.dataset.busy || p.status !== 'online') continue;` and the reschedule to `if (!once) setTimeout(thumbTick, 10000);`.

- [ ] **Step 5: History: final screenshot per run**

In `openTask`, inside the run card template, add after `<div class="res ...">...</div>`:
```js
      <img class="shot" alt="" loading="lazy" src="api/runs/${r.id}/shot.jpg" onerror="this.remove()" style="max-width:180px;border-radius:12px;margin:8px 0">
```

- [ ] **Step 6: Settings: OpenRouter only, management key, daily cap**

Delete `const FORMAT_PRESETS = {...};` and replace `openSettings` and `saveSettings` with:
```js
function openSettings() {
  const s = S.settings;
  $('so-title').textContent = 'Settings'; $('so-sub').textContent = 'OpenRouter keys, models and the limits for every task.';
  $('so-body').innerHTML = `
    <label for="s-key">OpenRouter API key <small>(the dashboard's own: splitting tasks, credit)</small></label>
    <input type="password" id="s-key" autocomplete="off" placeholder="${s.api_key_set ? 'Saved (' + esc(s.api_key_hint) + '). Type to replace.' : 'sk-or-v1-...'}">
    <label for="s-mkey">OpenRouter management key <small>(gives each phone its own capped key)</small></label>
    <input type="password" id="s-mkey" autocomplete="off" placeholder="${s.management_key_set ? 'Saved (' + esc(s.management_key_hint) + '). Type to replace.' : 'From openrouter.ai/settings/management-keys'}">
    <label for="s-cap">Daily AI budget per phone (USD)</label><input type="number" id="s-cap" min="0.05" max="100" step="0.05" value="${esc(s.daily_cap_usd)}">
    <div class="field-row">
      <div><label for="s-planner">Planner model</label><input type="text" id="s-planner" value="${esc(s.planner_model)}"></div>
      <div><label for="s-exec">Executor model</label><input type="text" id="s-exec" value="${esc(s.executor_model)}"></div>
    </div>
    <p class="field-note">Each phone runs its own agent and pays with its own key. The planner writes the goals, the executor does them.</p>
    <div class="field-row">
      <div><label for="s-steps">Default max steps</label><input type="number" id="s-steps" min="3" max="200" value="${esc(s.max_steps)}"></div>
      <div><label for="s-timeout">Time limit (minutes)</label><input type="number" id="s-timeout" min="1" max="240" value="${esc(s.timeout_minutes)}"></div>
    </div>
    <label class="switch" style="margin-top:14px"><input type="checkbox" id="s-vision" ${s.vision === '1' ? 'checked' : ''}><span class="sw"></span>
      <span><b>Send a small screenshot with every step</b><small>Helps on apps with poor accessibility labels. Costs a little more.</small></span></label>
    <p class="test-out" id="s-test-out"></p>`;
  const foot = $('so-foot'); foot.innerHTML = '';
  const test = el('button', 'secondary', 'Test connection');
  const save = el('button', 'primary', 'Save');
  test.onclick = async () => {
    const out = $('s-test-out'); out.className = 'test-out'; out.textContent = 'Testing...';
    try { await saveSettings(); const r = await api('api/settings/test', {method: 'POST'});
      const low = r.credit != null && r.credit < 0.5;
      const bal = r.credit != null ? ` Credit left: $${r.credit.toFixed(2)}.` : '';
      out.className = 'test-out ' + (r.ok && !low ? 'ok' : 'bad');
      out.textContent = (r.ok ? 'Connected. The planner model answered.' : r.error) + bal + (low ? ' Too low for tasks: add credit.' : '');
    } catch (e) { out.className = 'test-out bad'; out.textContent = e.message; }
  };
  save.onclick = async () => { try { await saveSettings(); toast('Settings saved'); closeSo(); } catch (e) { fail(e); } };
  foot.append(test, save);
  openSo();
}
async function saveSettings() {
  const body = {
    planner_model: $('s-planner').value, executor_model: $('s-exec').value, daily_cap_usd: $('s-cap').value,
    max_steps: $('s-steps').value, timeout_minutes: $('s-timeout').value, vision: $('s-vision').checked ? '1' : '0',
  };
  if ($('s-key').value.trim()) body.api_key = $('s-key').value.trim();
  if ($('s-mkey').value.trim()) body.management_key = $('s-mkey').value.trim();
  S.settings = await api('api/settings', {method: 'PUT', body});
  $('s-key').value = ''; $('s-mkey').value = ''; $('opt-steps').value = S.settings.max_steps;
  try { S.credit = await api('api/credit/refresh', {method: 'POST'}); renderCredit(); syncSend(); } catch (e) {}
}
```
In `boot`, change the first-run tip to `toast('Tip: add the OpenRouter API key and management key in Settings (gear icon).')`.

- [ ] **Step 7: Run all tests**

Run: `pytest -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add -A && git commit -q -m "v2 screens: OpenRouter settings with management key and daily cap, task tiles without video, final screenshots"
```

### Task 12: App agent types and `ScreenReader`

App tasks run in `~/claude_projects/fa-portal-v2`. `gradle-test X` means `export JAVA_HOME=$(mise where java@temurin-17) ANDROID_HOME=$PWD/.android-sdk && ./gradlew -q testDebugUnitTest --tests '*X'`. Kotlin files go in `app/src/main/java/com/mobilerun/portal/agent/`, tests in `app/src/test/java/com/mobilerun/portal/agent/`.

**Files:**
- Create: `agent/Model.kt`, `agent/ScreenReader.kt`
- Test: `agent/ScreenReaderTest.kt`, `agent/RunSpecTest.kt`

**Interfaces:**
- Produces:
  - data classes `Element(index, className, text, description, resourceId, left, top, right, bottom, clickable, editable, scrollable)` with `centerX`/`centerY`, and `Screen(app, packageName, keyboardVisible, width, height, elements, screenshotBase64: String? = null)`
  - `Prompts(version, planner, executor, tools: JSONArray)`
  - `RunSpec(uuid, instruction, reasoning, maxSteps, timeLimitMs, vision, plannerModel, executorModel, baseUrl, prompts)` with `RunSpec.fromJson(JSONObject)`
  - `ToolCall(name, args: JSONObject)`, `ActionOutcome(ok, text, done = false, success = false, answer = "")`
  - `enum RunStatus(wire)` with values SUCCEEDED, FAILED, STOPPED
  - `fun interface EventSink { fun event(kind: String, text: String, steps: Int?) }`
  - `object ScreenReader { const val MAX_ELEMENTS = 150; fun parse(state: JSONObject): Screen; fun describe(screen: Screen): String }`

- [ ] **Step 1: Write the failing tests**

`ScreenReaderTest.kt`:
```kotlin
package com.mobilerun.portal.agent

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ScreenReaderTest {
    private val state = JSONObject(
        """{"a11y_tree": {"className":"android.widget.FrameLayout","text":"","contentDescription":"","resourceId":"",
             "isScrollable":true,"isVisibleToUser":true,"boundsInScreen":{"left":0,"top":0,"right":1080,"bottom":2400},
             "children":[
               {"className":"android.widget.Button","text":"Sign in","contentDescription":"","resourceId":"com.x:id/login",
                "isClickable":true,"isVisibleToUser":true,"boundsInScreen":{"left":40,"top":300,"right":680,"bottom":380}},
               {"className":"android.widget.EditText","text":"","contentDescription":"Search","resourceId":"",
                "isEditable":true,"isClickable":true,"isVisibleToUser":true,"boundsInScreen":{"left":0,"top":100,"right":1080,"bottom":200}},
               {"className":"android.view.View","text":"","contentDescription":"","resourceId":"",
                "isVisibleToUser":true,"boundsInScreen":{"left":0,"top":0,"right":10,"bottom":10}},
               {"className":"android.widget.TextView","text":"Hidden","isVisibleToUser":false,
                "boundsInScreen":{"left":0,"top":0,"right":100,"bottom":100}}
             ]},
           "phone_state":{"currentApp":"Chrome","packageName":"com.android.chrome","keyboardVisible":true},
           "device_context":{"screen_bounds":{"width":1080,"height":2400}}}""",
    )

    @Test
    fun `keeps what the agent can use and numbers it`() {
        val screen = ScreenReader.parse(state)
        assertEquals(listOf(1, 2, 3), screen.elements.map { it.index })
        val button = screen.elements[1]
        assertEquals(Triple("Button", "Sign in", "login"), Triple(button.className, button.text, button.resourceId))
        assertEquals(360 to 340, button.centerX to button.centerY)
        assertEquals(Triple("Chrome", 1080, 2400), Triple(screen.app, screen.width, screen.height))
    }

    @Test
    fun `describes the screen as numbered lines`() {
        val text = ScreenReader.describe(ScreenReader.parse(state))
        assertTrue(text, text.startsWith("App: Chrome (com.android.chrome), keyboard open"))
        assertTrue(text, text.contains("2 Button \"Sign in\" id=login [tap] @360,340"))
        assertTrue(text, text.contains("3 EditText desc=\"Search\" [tap,edit] @540,150"))
        assertTrue(text, !text.contains("Hidden"))
    }

    @Test
    fun `an unreadable screen says so`() {
        val text = ScreenReader.describe(ScreenReader.parse(JSONObject("{}")))
        assertTrue(text, text.contains("(nothing readable"))
    }
}
```
`RunSpecTest.kt`:
```kotlin
package com.mobilerun.portal.agent

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Test

class RunSpecTest {
    @Test
    fun `reads the dashboard's agent run message`() {
        val spec = RunSpec.fromJson(
            JSONObject(
                """{"uuid":"u1","instruction":"open settings","reasoning":true,"max_steps":12,"time_limit_s":900,
                    "vision":false,"planner_model":"p/m","executor_model":"e/m","base_url":"https://openrouter.ai/api/v1/",
                    "prompts":{"version":"v","planner":"P","executor":"E","tools":[{"type":"function"}]}}""",
            ),
        )
        assertEquals("u1", spec.uuid)
        assertEquals(12, spec.maxSteps)
        assertEquals(900_000L, spec.timeLimitMs)
        assertEquals("https://openrouter.ai/api/v1", spec.baseUrl)
        assertEquals(1, spec.prompts.tools.length())
    }
}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `gradle-test ScreenReaderTest` (and `RunSpecTest`)
Expected: compile errors (types missing).

- [ ] **Step 3: Write `Model.kt`**

```kotlin
package com.mobilerun.portal.agent

import org.json.JSONArray
import org.json.JSONObject

/** Something on the screen the agent can refer to by its number. */
data class Element(
    val index: Int,
    val className: String,
    val text: String,
    val description: String,
    val resourceId: String,
    val left: Int,
    val top: Int,
    val right: Int,
    val bottom: Int,
    val clickable: Boolean,
    val editable: Boolean,
    val scrollable: Boolean,
) {
    val centerX: Int get() = (left + right) / 2
    val centerY: Int get() = (top + bottom) / 2
}

data class Screen(
    val app: String,
    val packageName: String,
    val keyboardVisible: Boolean,
    val width: Int,
    val height: Int,
    val elements: List<Element>,
    val screenshotBase64: String? = null,
)

/** Instructions and tools from the dashboard (FastAutomate v2: sent with every task). */
data class Prompts(val version: String, val planner: String, val executor: String, val tools: JSONArray)

data class RunSpec(
    val uuid: String,
    val instruction: String,
    val reasoning: Boolean,
    val maxSteps: Int,
    val timeLimitMs: Long,
    val vision: Boolean,
    val plannerModel: String,
    val executorModel: String,
    val baseUrl: String,
    val prompts: Prompts,
) {
    companion object {
        /** The dashboard's agent/run params (or the saved defaults plus a uuid and an instruction). */
        fun fromJson(json: JSONObject): RunSpec {
            val p = json.getJSONObject("prompts")
            return RunSpec(
                uuid = json.getString("uuid"),
                instruction = json.getString("instruction"),
                reasoning = json.optBoolean("reasoning", true),
                maxSteps = json.optInt("max_steps", 30).coerceIn(1, 200),
                timeLimitMs = json.optLong("time_limit_s", 900L).coerceIn(60L, 14_400L) * 1000,
                vision = json.optBoolean("vision", true),
                plannerModel = json.getString("planner_model"),
                executorModel = json.getString("executor_model"),
                baseUrl = json.optString("base_url", "https://openrouter.ai/api/v1").trimEnd('/'),
                prompts = Prompts(p.optString("version"), p.getString("planner"), p.getString("executor"), p.getJSONArray("tools")),
            )
        }
    }
}

data class ToolCall(val name: String, val args: JSONObject)

data class ActionOutcome(
    val ok: Boolean,
    val text: String,
    val done: Boolean = false,
    val success: Boolean = false,
    val answer: String = "",
)

enum class RunStatus(val wire: String) { SUCCEEDED("succeeded"), FAILED("failed"), STOPPED("stopped") }

/** Where a run reports progress; AgentHost numbers and sends each report. */
fun interface EventSink {
    fun event(kind: String, text: String, steps: Int?)
}
```

- [ ] **Step 4: Write `ScreenReader.kt`**

```kotlin
package com.mobilerun.portal.agent

import org.json.JSONObject

/** Turns the app's accessibility state ("state" with filter=true) into the numbered list the agent reads. */
object ScreenReader {
    const val MAX_ELEMENTS = 150

    fun parse(state: JSONObject): Screen {
        val phone = state.optJSONObject("phone_state") ?: JSONObject()
        val bounds = state.optJSONObject("device_context")?.optJSONObject("screen_bounds")
        val elements = ArrayList<Element>()
        state.optJSONObject("a11y_tree")?.let { collect(it, elements) }
        return Screen(
            app = phone.optString("currentApp"),
            packageName = phone.optString("packageName"),
            keyboardVisible = phone.optBoolean("keyboardVisible"),
            width = bounds?.optInt("width") ?: 0,
            height = bounds?.optInt("height") ?: 0,
            elements = elements,
        )
    }

    private fun collect(node: JSONObject, out: MutableList<Element>) {
        if (out.size >= MAX_ELEMENTS) return
        val b = node.optJSONObject("boundsInScreen")
        val left = b?.optInt("left") ?: 0
        val top = b?.optInt("top") ?: 0
        val right = b?.optInt("right") ?: 0
        val bottom = b?.optInt("bottom") ?: 0
        val text = node.optString("text").trim()
        val desc = node.optString("contentDescription").trim()
        val clickable = node.optBoolean("isClickable") || node.optBoolean("isLongClickable")
        val editable = node.optBoolean("isEditable")
        val scrollable = node.optBoolean("isScrollable")
        val visible = node.optBoolean("isVisibleToUser", true) && right > left && bottom > top
        if (visible && (clickable || editable || scrollable || text.isNotEmpty() || desc.isNotEmpty())) {
            out += Element(
                index = out.size + 1,
                className = node.optString("className").substringAfterLast('.'),
                text = text.take(80),
                description = desc.take(80),
                resourceId = node.optString("resourceId").substringAfter(":id/", ""),
                left = left, top = top, right = right, bottom = bottom,
                clickable = clickable, editable = editable, scrollable = scrollable,
            )
        }
        val children = node.optJSONArray("children") ?: return
        for (i in 0 until children.length()) {
            children.optJSONObject(i)?.let { collect(it, out) }
        }
    }

    fun describe(screen: Screen): String = buildString {
        append("App: ").append(screen.app).append(" (").append(screen.packageName).append(')')
        if (screen.keyboardVisible) append(", keyboard open")
        append("\nScreen: ").append(screen.width).append('x').append(screen.height).append("\nElements:\n")
        if (screen.elements.isEmpty()) append("(nothing readable: use the screenshot and tap_at)\n")
        for (e in screen.elements) {
            append(e.index).append(' ').append(e.className)
            if (e.text.isNotEmpty()) append(" \"").append(e.text).append('"')
            if (e.description.isNotEmpty() && e.description != e.text) append(" desc=\"").append(e.description).append('"')
            if (e.resourceId.isNotEmpty()) append(" id=").append(e.resourceId)
            val flags = listOfNotNull("tap".takeIf { e.clickable }, "edit".takeIf { e.editable }, "scroll".takeIf { e.scrollable })
            if (flags.isNotEmpty()) append(" [").append(flags.joinToString(",")).append(']')
            append(" @").append(e.centerX).append(',').append(e.centerY).append('\n')
        }
    }
}
```

- [ ] **Step 5: Run the tests**

Run: `gradle-test ScreenReaderTest` and `gradle-test RunSpecTest`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/src/main/java/com/mobilerun/portal/agent app/src/test/java/com/mobilerun/portal/agent && git commit -q -m "agent: run spec, screen elements and the numbered screen description"
```

### Task 13: `PhoneControl` (the agent's hands, through the app's own `ActionDispatcher`)

**Files:**
- Create: `agent/PhoneControl.kt`
- Test: `agent/DispatcherPhoneControlTest.kt`

**Interfaces:**
- Consumes: `ActionDispatcher.dispatch(action, params, origin, requestId)` and `ApiResponse` (Success, Error, RawObject, RawArray, Text).
- Produces:
  - `interface PhoneControl` with:
    - `fun readScreen(): JSONObject?`
    - `fun screenshot(maxSide: Int, quality: Int): String?` (base64 JPEG)
    - `fun tap(x: Int, y: Int): String?`
    - `fun swipe(x1: Int, y1: Int, x2: Int, y2: Int, durationMs: Int): String?`
    - `fun type(text: String, clear: Boolean): String?`
    - `fun key(keyCode: Int): String?`
    - `fun global(action: Int): String?`
    - `fun launchableApps(): List<Pair<String, String>>` (label to package)
    - `fun launch(packageName: String): String?`
    - `fun sleep(ms: Long)`

    The actions return `null` for OK, otherwise the error text.
  - `class DispatcherPhoneControl(dispatcher: () -> ActionDispatcher?) : PhoneControl`

- [ ] **Step 1: Write the failing test**

`DispatcherPhoneControlTest.kt`:
```kotlin
package com.mobilerun.portal.agent

import com.mobilerun.portal.api.ApiResponse
import com.mobilerun.portal.service.ActionDispatcher
import io.mockk.every
import io.mockk.mockk
import io.mockk.slot
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class DispatcherPhoneControlTest {
    private val dispatcher = mockk<ActionDispatcher>()
    private val control = DispatcherPhoneControl { dispatcher }

    @Test
    fun `actions go to the local dispatcher and errors come back as text`() {
        val params = slot<JSONObject>()
        every { dispatcher.dispatch("tap", capture(params), any(), any()) } returns ApiResponse.Success("ok")
        every { dispatcher.dispatch("global", any(), any(), any()) } returns ApiResponse.Error("Failed to perform global action 9")
        assertNull(control.tap(10, 20))
        assertEquals(10 to 20, params.captured.getInt("x") to params.captured.getInt("y"))
        assertEquals("Failed to perform global action 9", control.global(9))
    }

    @Test
    fun `typing sends base64 text and reading returns the state`() {
        val params = slot<JSONObject>()
        every { dispatcher.dispatch("keyboard/input", capture(params), any(), any()) } returns ApiResponse.Success("ok")
        every { dispatcher.dispatch("state", any(), any(), any()) } returns ApiResponse.RawObject(JSONObject().put("a11y_tree", JSONObject()))
        every { dispatcher.dispatch("screenshot", any(), any(), any()) } returns ApiResponse.Text("BASE64JPEG")
        assertNull(control.type("héllo", clear = true))
        assertEquals("héllo", String(java.util.Base64.getDecoder().decode(params.captured.getString("base64_text"))))
        assertEquals(true, control.readScreen()!!.has("a11y_tree"))
        assertEquals("BASE64JPEG", control.screenshot(960, 60))
    }

    @Test
    fun `apps are listed by label`() {
        val apps = JSONArray().put(JSONObject().put("label", "Settings").put("packageName", "com.android.settings"))
        every { dispatcher.dispatch("packages", any(), any(), any()) } returns ApiResponse.RawArray(apps)
        assertEquals(listOf("Settings" to "com.android.settings"), control.launchableApps())
    }

    @Test
    fun `no accessibility service means readable errors`() {
        val off = DispatcherPhoneControl { null }
        assertEquals("Turn on FastAutomate v2 in Accessibility", off.tap(1, 1))
        assertNull(off.readScreen())
    }
}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `gradle-test DispatcherPhoneControlTest`
Expected: compile error (`DispatcherPhoneControl` missing).

- [ ] **Step 3: Write `PhoneControl.kt`**

```kotlin
package com.mobilerun.portal.agent

import com.mobilerun.portal.api.ApiResponse
import com.mobilerun.portal.service.ActionDispatcher
import org.json.JSONArray
import org.json.JSONObject

/** What the agent can do on its phone. Actions return null when they worked, else the error text. */
interface PhoneControl {
    fun readScreen(): JSONObject?
    fun screenshot(maxSide: Int, quality: Int): String?
    fun tap(x: Int, y: Int): String?
    fun swipe(x1: Int, y1: Int, x2: Int, y2: Int, durationMs: Int): String?
    fun type(text: String, clear: Boolean): String?
    fun key(keyCode: Int): String?
    fun global(action: Int): String?
    fun launchableApps(): List<Pair<String, String>>
    fun launch(packageName: String): String?
    fun sleep(ms: Long) = Thread.sleep(ms)
}

/** The same actions the dashboard used to call remotely, now called locally. */
class DispatcherPhoneControl(private val dispatcher: () -> ActionDispatcher?) : PhoneControl {
    private fun call(method: String, params: JSONObject): ApiResponse =
        dispatcher()?.dispatch(method, params, ActionDispatcher.Origin.WEBSOCKET_LOCAL)
            ?: ApiResponse.Error(NO_SERVICE)

    private fun problem(response: ApiResponse): String? = (response as? ApiResponse.Error)?.message

    override fun readScreen(): JSONObject? = when (val r = call("state", JSONObject().put("filter", true))) {
        is ApiResponse.RawObject -> r.json
        is ApiResponse.Success -> (r.data as? String)?.let { runCatching { JSONObject(it) }.getOrNull() }
        else -> null
    }

    override fun screenshot(maxSide: Int, quality: Int): String? {
        val params = JSONObject().put("format", "jpeg").put("maxSide", maxSide).put("quality", quality).put("hideOverlay", true)
        return (call("screenshot", params) as? ApiResponse.Text)?.data
    }

    override fun tap(x: Int, y: Int) = problem(call("tap", JSONObject().put("x", x).put("y", y)))

    override fun swipe(x1: Int, y1: Int, x2: Int, y2: Int, durationMs: Int) = problem(
        call("swipe", JSONObject().put("startX", x1).put("startY", y1).put("endX", x2).put("endY", y2).put("duration", durationMs)),
    )

    override fun type(text: String, clear: Boolean): String? {
        val encoded = java.util.Base64.getEncoder().encodeToString(text.toByteArray(Charsets.UTF_8))
        return problem(call("keyboard/input", JSONObject().put("base64_text", encoded).put("clear", clear)))
    }

    override fun key(keyCode: Int) = problem(call("keyboard/key", JSONObject().put("key_code", keyCode)))

    override fun global(action: Int) = problem(call("global", JSONObject().put("action", action)))

    override fun launchableApps(): List<Pair<String, String>> {
        val list: JSONArray = when (val r = call("packages", JSONObject())) {
            is ApiResponse.RawArray -> r.json
            else -> return emptyList()
        }
        return (0 until list.length()).mapNotNull { i ->
            list.optJSONObject(i)?.let { it.optString("label") to it.optString("packageName") }
        }.filter { it.second.isNotEmpty() }
    }

    override fun launch(packageName: String) = problem(call("app", JSONObject().put("package", packageName)))

    companion object {
        const val NO_SERVICE = "Turn on FastAutomate v2 in Accessibility"
    }
}
```

- [ ] **Step 4: Run the test**

Run: `gradle-test DispatcherPhoneControlTest`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -q -m "agent: phone control through the app's own action dispatcher"
```

### Task 14: `LlmClient` (OpenRouter chat completions with tools)

**Files:**
- Create: `agent/LlmClient.kt`
- Test: `agent/LlmClientTest.kt`

**Interfaces:**
- Produces:
  - `data class HttpResult(val code: Int, val body: String)`, where code -1 means a network failure
  - `fun interface LlmTransport { fun post(url: String, headers: Map<String, String>, body: String, timeoutMs: Long): HttpResult }`
  - `class OkHttpTransport : LlmTransport`
  - `class LlmError(message: String, val budget: Boolean = false) : Exception(message)`
  - `sealed class LlmReply { data class Tool(val call: ToolCall, val thought: String); data class Text(val text: String) }`
  - `class LlmClient(transport: LlmTransport, baseUrl: String, apiKey: () -> String?, sleep: (Long) -> Unit = { Thread.sleep(it) })` with:
    - `fun complete(model: String, messages: JSONArray, tools: JSONArray?, maxTokens: Int = 1024): LlmReply`
    - `companion { const val BUDGET_MESSAGE; const val ACCOUNT_EMPTY_MESSAGE; fun parse(body: String): LlmReply }`

- [ ] **Step 1: Write the failing tests**

`LlmClientTest.kt`:
```kotlin
package com.mobilerun.portal.agent

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

fun toolReply(name: String, args: String, thought: String = "") = HttpResult(
    200,
    JSONObject().put(
        "choices",
        JSONArray().put(
            JSONObject().put(
                "message",
                JSONObject().put("content", thought).put(
                    "tool_calls",
                    JSONArray().put(JSONObject().put("id", "c1").put("type", "function")
                        .put("function", JSONObject().put("name", name).put("arguments", args))),
                ),
            ),
        ),
    ).toString(),
)

fun textReply(text: String) = HttpResult(
    200,
    JSONObject().put("choices", JSONArray().put(JSONObject().put("message", JSONObject().put("content", text)))).toString(),
)

class ScriptedTransport(vararg replies: HttpResult) : LlmTransport {
    val queue = ArrayDeque(replies.toList())
    val requests = mutableListOf<Pair<Map<String, String>, JSONObject>>()
    override fun post(url: String, headers: Map<String, String>, body: String, timeoutMs: Long): HttpResult {
        requests += headers to JSONObject(body)
        return queue.removeFirstOrNull() ?: textReply("(script ended)")
    }
}

class LlmClientTest {
    private val messages = JSONArray().put(JSONObject().put("role", "user").put("content", "hi"))
    private val tools = JSONArray().put(JSONObject().put("type", "function"))

    @Test
    fun `a tool call comes back as a tool reply`() {
        val transport = ScriptedTransport(toolReply("tap", """{"index":3}""", "Tap the button"))
        val reply = LlmClient(transport, "https://openrouter.ai/api/v1", { "sk-or-v1-phone" }).complete("e/m", messages, tools)
        reply as LlmReply.Tool
        assertEquals("tap", reply.call.name)
        assertEquals(3, reply.call.args.getInt("index"))
        assertEquals("Tap the button", reply.thought)
        val (headers, body) = transport.requests.single()
        assertEquals("Bearer sk-or-v1-phone", headers["Authorization"])
        assertEquals("required", body.getString("tool_choice"))
        assertEquals("e/m", body.getString("model"))
    }

    @Test
    fun `unreadable arguments become a text reply`() {
        val reply = LlmClient.parse(toolReply("tap", "{index:").body)
        assertTrue(reply is LlmReply.Text && reply.text.contains("Unreadable arguments for tap"))
    }

    @Test
    fun `key limit is a budget error`() {
        val limit = HttpResult(403, """{"error":{"message":"Key limit exceeded (daily limit)"}}""")
        try {
            LlmClient(ScriptedTransport(limit), "u", { "k" }).complete("m", messages, tools)
            fail("expected a budget error")
        } catch (e: LlmError) {
            assertTrue(e.budget)
            assertEquals(LlmClient.BUDGET_MESSAGE, e.message)
        }
    }

    @Test
    fun `empty account is a budget error with its own message`() {
        try {
            LlmClient(ScriptedTransport(HttpResult(402, "{}")), "u", { "k" }).complete("m", messages, tools)
            fail("expected a budget error")
        } catch (e: LlmError) {
            assertEquals(LlmClient.ACCOUNT_EMPTY_MESSAGE, e.message)
        }
    }

    @Test
    fun `busy and failing servers are retried twice`() {
        val transport = ScriptedTransport(HttpResult(429, "{}"), HttpResult(-1, "timeout"), textReply("ok"))
        val slept = mutableListOf<Long>()
        val reply = LlmClient(transport, "u", { "k" }, { slept += it }).complete("m", messages, null)
        assertEquals(LlmReply.Text("ok"), reply)
        assertEquals(listOf(1500L, 3000L), slept)
    }

    @Test
    fun `no key yet is a readable error`() {
        try {
            LlmClient(ScriptedTransport(), "u", { null }).complete("m", messages, null)
            fail("expected an error")
        } catch (e: LlmError) {
            assertTrue(e.message!!.contains("no AI key"))
        }
    }
}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `gradle-test LlmClientTest`
Expected: compile errors.

- [ ] **Step 3: Write `LlmClient.kt`**

```kotlin
package com.mobilerun.portal.agent

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONException
import org.json.JSONObject
import java.util.concurrent.TimeUnit

data class HttpResult(val code: Int, val body: String)

fun interface LlmTransport {
    fun post(url: String, headers: Map<String, String>, body: String, timeoutMs: Long): HttpResult
}

class OkHttpTransport : LlmTransport {
    private val client = OkHttpClient.Builder().connectTimeout(15, TimeUnit.SECONDS).build()

    override fun post(url: String, headers: Map<String, String>, body: String, timeoutMs: Long): HttpResult {
        val request = Request.Builder().url(url)
            .apply { headers.forEach { (k, v) -> header(k, v) } }
            .post(body.toRequestBody("application/json".toMediaType()))
            .build()
        return try {
            client.newBuilder().callTimeout(timeoutMs, TimeUnit.MILLISECONDS).build()
                .newCall(request).execute().use { HttpResult(it.code, it.body?.string().orEmpty()) }
        } catch (e: java.io.IOException) {
            HttpResult(-1, e.message ?: "network error")
        }
    }
}

class LlmError(message: String, val budget: Boolean = false) : Exception(message)

sealed class LlmReply {
    data class Tool(val call: ToolCall, val thought: String) : LlmReply()
    data class Text(val text: String) : LlmReply()
}

/** OpenRouter (OpenAI format) chat completions, paid with this phone's own key. */
class LlmClient(
    private val transport: LlmTransport,
    private val baseUrl: String,
    private val apiKey: () -> String?,
    private val sleep: (Long) -> Unit = { Thread.sleep(it) },
) {
    fun complete(model: String, messages: JSONArray, tools: JSONArray?, maxTokens: Int = 1024): LlmReply {
        val key = apiKey() ?: throw LlmError("This phone has no AI key yet: keep it connected to the dashboard for a moment.")
        val body = JSONObject().put("model", model).put("messages", messages).put("max_tokens", maxTokens)
        if (tools != null) body.put("tools", tools).put("tool_choice", "required")
        val headers = mapOf("Authorization" to "Bearer $key", "Content-Type" to "application/json", "X-Title" to "FastAutomate v2")
        var attempt = 0
        while (true) {
            val r = transport.post("$baseUrl/chat/completions", headers, body.toString(), 60_000)
            when {
                r.code == 200 -> return parse(r.body)
                r.code == 402 -> throw LlmError(ACCOUNT_EMPTY_MESSAGE, budget = true)
                r.code == 403 && r.body.contains("limit", ignoreCase = true) -> throw LlmError(BUDGET_MESSAGE, budget = true)
                r.code == 401 -> throw LlmError("This phone's AI key was refused. Reconnect it to the dashboard to get a new one.")
                (r.code == 429 || r.code >= 500 || r.code == -1) && attempt < 2 -> {
                    attempt++
                    sleep(1500L * attempt)
                }
                else -> throw LlmError("The AI said ${r.code}: ${errorText(r.body)}")
            }
        }
    }

    companion object {
        const val BUDGET_MESSAGE = "This phone's daily AI budget is used up"
        const val ACCOUNT_EMPTY_MESSAGE = "The OpenRouter account is out of credit"

        fun parse(body: String): LlmReply {
            val message = JSONObject(body).getJSONArray("choices").getJSONObject(0).getJSONObject("message")
            val thought = if (message.isNull("content")) "" else message.optString("content").trim()
            val calls = message.optJSONArray("tool_calls")
            if (calls == null || calls.length() == 0) return LlmReply.Text(thought)
            val function = calls.getJSONObject(0).getJSONObject("function")
            val raw = function.optString("arguments").ifBlank { "{}" }
            val args = try {
                JSONObject(raw)
            } catch (e: JSONException) {
                return LlmReply.Text("Unreadable arguments for ${function.optString("name")}: ${raw.take(200)}")
            }
            return LlmReply.Tool(ToolCall(function.getString("name"), args), thought)
        }

        private fun errorText(body: String): String =
            runCatching { JSONObject(body).getJSONObject("error").getString("message") }.getOrDefault(body.take(200))
    }
}
```

- [ ] **Step 4: Run the tests**

Run: `gradle-test LlmClientTest`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -q -m "agent: OpenRouter client with tools, retries and readable budget errors"
```

### Task 15: `Actions` (one tool call, done on the phone)

**Files:**
- Create: `agent/Actions.kt`
- Test: `agent/ActionsTest.kt`, `agent/FakePhone.kt` (test helper, also used in Tasks 16 and 18)

**Interfaces:**
- Consumes: `PhoneControl` (Task 13), `ToolCall`, `Screen`, `Element`, `ActionOutcome` (Task 12).
- Produces: `class Actions(phone: PhoneControl) { fun run(call: ToolCall, screen: Screen): ActionOutcome }`. The tool names and arguments are exactly the dashboard's `TOOLS`. Constants `GLOBAL_BACK = 1`, `GLOBAL_HOME = 2`, `KEYCODE_ENTER = 66`.

- [ ] **Step 1: Write the test helper and the failing tests**

`FakePhone.kt` (in the test folder):
```kotlin
package com.mobilerun.portal.agent

import org.json.JSONObject

/** A phone for tests: records what the agent does and shows a scripted screen. */
class FakePhone(var state: JSONObject = JSONObject(), var shot: String? = "SHOT") : PhoneControl {
    val done = mutableListOf<String>()
    var failTaps = false
    var apps = listOf("Settings" to "com.android.settings", "Chrome" to "com.android.chrome")

    override fun readScreen() = state
    override fun screenshot(maxSide: Int, quality: Int) = shot.also { done += "shot $maxSide" }
    override fun tap(x: Int, y: Int): String? { done += "tap $x,$y"; return if (failTaps) "Failed to perform tap" else null }
    override fun swipe(x1: Int, y1: Int, x2: Int, y2: Int, durationMs: Int): String? { done += "swipe $x1,$y1>$x2,$y2"; return null }
    override fun type(text: String, clear: Boolean): String? { done += "type $text"; return null }
    override fun key(keyCode: Int): String? { done += "key $keyCode"; return null }
    override fun global(action: Int): String? { done += "global $action"; return null }
    override fun launchableApps() = apps
    override fun launch(packageName: String): String? { done += "launch $packageName"; return null }
    override fun sleep(ms: Long) { done += "sleep $ms" }
}
```
`ActionsTest.kt`:
```kotlin
package com.mobilerun.portal.agent

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ActionsTest {
    private val field = Element(1, "EditText", "", "Search", "", 0, 100, 1080, 200, clickable = true, editable = true, scrollable = false)
    private val list = Element(2, "RecyclerView", "", "", "list", 0, 300, 1080, 2300, clickable = false, editable = false, scrollable = true)
    private val screen = Screen("Chrome", "com.android.chrome", false, 1080, 2400, listOf(field, list))
    private val phone = FakePhone()
    private val actions = Actions(phone)
    private fun call(name: String, args: String = "{}") = actions.run(ToolCall(name, JSONObject(args)), screen)

    @Test
    fun `tap an element by its number`() {
        assertTrue(call("tap", """{"index":1}""").ok)
        assertEquals(listOf("tap 540,150"), phone.done)
    }

    @Test
    fun `missing element is a failed step`() {
        val outcome = call("tap", """{"index":42}""")
        assertFalse(outcome.ok)
        assertEquals("Element 42 is not on the screen", outcome.text)
        assertTrue(phone.done.isEmpty())
    }

    @Test
    fun `typing into a numbered field focuses it first`() {
        assertTrue(call("type", """{"text":"weather","index":1}""").ok)
        assertEquals(listOf("tap 540,150", "sleep 400", "type weather"), phone.done)
    }

    @Test
    fun `scroll down inside a list moves the finger up`() {
        assertTrue(call("scroll", """{"direction":"down","index":2}""").ok)
        assertEquals(listOf("swipe 540,1900>540,700"), phone.done)
    }

    @Test
    fun `tap_at stays on the screen`() {
        call("tap_at", """{"x":5000,"y":-3}""")
        assertEquals(listOf("tap 1079,0"), phone.done)
    }

    @Test
    fun `keys, apps, waiting and done`() {
        assertTrue(call("back").ok && call("home").ok && call("enter").ok)
        assertTrue(call("open_app", """{"name":"settings"}""").ok)
        assertFalse(call("open_app", """{"name":"Nope"}""").ok)
        assertTrue(call("wait", """{"seconds":50}""").ok)
        assertEquals(listOf("global 1", "global 2", "key 66", "launch com.android.settings", "sleep 10000"), phone.done)
        val done = call("done", """{"success":true,"answer":"24 C"}""")
        assertTrue(done.done && done.success)
        assertEquals("24 C", done.answer)
    }

    @Test
    fun `unknown tools and bad arguments are failed steps`() {
        assertFalse(call("fly").ok)
        assertEquals("Bad arguments for tap", call("tap", """{"index":"x"}""").text.substringBefore(':'))
    }

    @Test
    fun `a refused tap is reported`() {
        phone.failTaps = true
        val outcome = call("tap", """{"index":1}""")
        assertFalse(outcome.ok)
        assertEquals("Failed to perform tap", outcome.text)
    }
}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `gradle-test ActionsTest`
Expected: compile error (`Actions` missing).

- [ ] **Step 3: Write `Actions.kt`**

```kotlin
package com.mobilerun.portal.agent

import org.json.JSONException

/** Performs one tool call from the model. Tool names and arguments match the dashboard's TOOLS. */
class Actions(private val phone: PhoneControl) {
    fun run(call: ToolCall, screen: Screen): ActionOutcome = try {
        when (call.name) {
            "tap" -> withElement(call, screen) { e -> result(phone.tap(e.centerX, e.centerY), "Tapped ${label(e)}") }
            "tap_at" -> {
                val (x, y) = onScreen(call.args.getInt("x"), call.args.getInt("y"), screen)
                result(phone.tap(x, y), "Tapped at $x,$y")
            }
            "type" -> type(call, screen)
            "scroll" -> scroll(call, screen)
            "back" -> result(phone.global(GLOBAL_BACK), "Pressed Back")
            "home" -> result(phone.global(GLOBAL_HOME), "Went to the home screen")
            "enter" -> result(phone.key(KEYCODE_ENTER), "Pressed Enter")
            "open_app" -> openApp(call.args.getString("name"))
            "wait" -> {
                val seconds = call.args.optDouble("seconds", 2.0).coerceIn(0.5, 10.0)
                phone.sleep((seconds * 1000).toLong())
                ActionOutcome(true, "Waited ${seconds}s")
            }
            "done" -> {
                val answer = call.args.optString("answer")
                ActionOutcome(true, answer, done = true, success = call.args.optBoolean("success", false), answer = answer)
            }
            else -> ActionOutcome(false, "Unknown action ${call.name}")
        }
    } catch (e: JSONException) {
        ActionOutcome(false, "Bad arguments for ${call.name}: ${e.message}")
    }

    private fun withElement(call: ToolCall, screen: Screen, act: (Element) -> ActionOutcome): ActionOutcome {
        val index = call.args.getInt("index")
        val element = screen.elements.firstOrNull { it.index == index }
            ?: return ActionOutcome(false, "Element $index is not on the screen")
        return act(element)
    }

    private fun type(call: ToolCall, screen: Screen): ActionOutcome {
        val text = call.args.getString("text")
        if (call.args.has("index")) {
            val focus = withElement(call, screen) { e -> result(phone.tap(e.centerX, e.centerY), "Focused ${label(e)}") }
            if (!focus.ok) return focus
            phone.sleep(FOCUS_MS)
        }
        return result(phone.type(text, clear = true), "Typed \"${text.take(60)}\"")
    }

    private fun scroll(call: ToolCall, screen: Screen): ActionOutcome {
        val direction = call.args.getString("direction")
        val area = if (call.args.has("index")) {
            screen.elements.firstOrNull { it.index == call.args.getInt("index") }
                ?: return ActionOutcome(false, "Element ${call.args.getInt("index")} is not on the screen")
        } else {
            null
        }
        val left = area?.left ?: 0
        val top = area?.top ?: 0
        val right = area?.right ?: screen.width
        val bottom = area?.bottom ?: screen.height
        val cx = (left + right) / 2
        val cy = (top + bottom) / 2
        val dx = (right - left) * 3 / 10
        val dy = (bottom - top) * 3 / 10
        val (x1, y1, x2, y2) = when (direction) {
            "down" -> listOf(cx, cy + dy, cx, cy - dy)
            "up" -> listOf(cx, cy - dy, cx, cy + dy)
            "right" -> listOf(cx + dx, cy, cx - dx, cy)
            "left" -> listOf(cx - dx, cy, cx + dx, cy)
            else -> return ActionOutcome(false, "Unknown scroll direction $direction")
        }
        return result(phone.swipe(x1, y1, x2, y2, SCROLL_MS), "Scrolled $direction")
    }

    private fun openApp(name: String): ActionOutcome {
        val apps = phone.launchableApps()
        val match = apps.firstOrNull { it.first.equals(name, ignoreCase = true) }
            ?: apps.firstOrNull { it.first.contains(name, ignoreCase = true) }
            ?: apps.firstOrNull { it.second.equals(name, ignoreCase = true) }
            ?: return ActionOutcome(false, "No app called \"$name\" on this phone")
        return result(phone.launch(match.second), "Opened ${match.first}")
    }

    private fun onScreen(x: Int, y: Int, screen: Screen): Pair<Int, Int> =
        x.coerceIn(0, maxOf(0, screen.width - 1)) to y.coerceIn(0, maxOf(0, screen.height - 1))

    private fun result(problem: String?, success: String) =
        if (problem == null) ActionOutcome(true, success) else ActionOutcome(false, problem)

    private fun label(e: Element) = when {
        e.text.isNotEmpty() -> "\"${e.text}\""
        e.description.isNotEmpty() -> "\"${e.description}\""
        else -> "element ${e.index}"
    }

    companion object {
        const val GLOBAL_BACK = 1
        const val GLOBAL_HOME = 2
        const val KEYCODE_ENTER = 66
        const val FOCUS_MS = 400L
        const val SCROLL_MS = 350
    }
}
```
(The scroll test expects `swipe 540,1900>540,700`: the list covers 300 to 2300, so `cy` = 1300 and `dy` = 600.)

- [ ] **Step 4: Run the tests**

Run: `gradle-test ActionsTest`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -q -m "agent: actions (tap, type, scroll, keys, open app, wait, done) with on-screen checks"
```

### Task 16: `Planner` and `AgentLoop` (one run)

**Files:**
- Create: `agent/Planner.kt`, `agent/AgentLoop.kt`
- Test: `agent/AgentLoopTest.kt`

**Interfaces:**
- Consumes: `LlmClient`, `LlmReply`, `LlmError` (Task 14); `Actions` (Task 15); `ScreenReader` (Task 12); `PhoneControl` (Task 13).
- Produces:
  - `class Planner(llm: LlmClient) { fun plan(spec: RunSpec, screen: Screen, trouble: List<String>): List<String>; companion fun parseGoals(text: String): List<String>? }`
  - `class AgentLoop(spec: RunSpec, phone: PhoneControl, llm: LlmClient, sink: EventSink, clock: () -> Long = System::currentTimeMillis)` with `@Volatile var stopRequested`, `fun run(): Result`, `data class Result(status: RunStatus, result: String, steps: Int)`, `SETTLE_MS = 700L`

- [ ] **Step 1: Write the failing tests**

`AgentLoopTest.kt`:
```kotlin
package com.mobilerun.portal.agent

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class AgentLoopTest {
    private val state = JSONObject(
        """{"a11y_tree":{"className":"android.widget.Button","text":"Settings","isClickable":true,"isVisibleToUser":true,
            "boundsInScreen":{"left":0,"top":0,"right":200,"bottom":100}},
           "phone_state":{"currentApp":"Launcher","packageName":"l"},"device_context":{"screen_bounds":{"width":1080,"height":2400}}}""",
    )

    private fun spec(reasoning: Boolean = false, maxSteps: Int = 10, vision: Boolean = false) = RunSpec(
        uuid = "u1", instruction = "open settings", reasoning = reasoning, maxSteps = maxSteps, timeLimitMs = 60_000,
        vision = vision, plannerModel = "p/m", executorModel = "e/m", baseUrl = "u",
        prompts = Prompts("v", "PLAN", "EXEC", JSONArray().put(JSONObject().put("type", "function"))),
    )

    private val events = mutableListOf<Pair<String, String>>()
    private val sink = EventSink { kind, text, _ -> events += kind to text }

    private fun loop(spec: RunSpec, transport: LlmTransport, phone: FakePhone = FakePhone(state), clock: () -> Long = { 0L }) =
        AgentLoop(spec, phone, LlmClient(transport, "u", { "k" }, {}), sink, clock)

    @Test
    fun `finishes when the model calls done`() {
        val transport = ScriptedTransport(toolReply("tap", """{"index":1}"""), toolReply("done", """{"success":true,"answer":"Settings is open"}"""))
        val result = loop(spec(), transport).run()
        assertEquals(AgentLoop.Result(RunStatus.SUCCEEDED, "Settings is open", 2), result)
        assertTrue(events.contains("action" to "tap index=1"))
        assertTrue(events.contains("ok" to "Tapped \"Settings\""))
        assertTrue(events.contains("answer" to "Settings is open"))
        val request = transport.requests.first().second
        assertEquals("EXEC", request.getJSONArray("messages").getJSONObject(0).getString("content"))
    }

    @Test
    fun `model without a tool call is a failed step`() {
        val transport = ScriptedTransport(textReply("I think I should tap"), toolReply("done", """{"success":true,"answer":"ok"}"""))
        val result = loop(spec(), transport).run()
        assertEquals(RunStatus.SUCCEEDED, result.status)
        assertTrue(events.any { it.first == "error" && it.second.startsWith("The model answered without choosing an action") })
    }

    @Test
    fun `stops at the step limit`() {
        val transport = ScriptedTransport(*Array(5) { toolReply("wait", """{"seconds":1}""") })
        val result = loop(spec(maxSteps = 3), transport).run()
        assertEquals(RunStatus.FAILED, result.status)
        assertEquals(3, result.steps)
        assertTrue(result.result.contains("step limit"))
    }

    @Test
    fun `a stop request ends the run`() {
        val transport = ScriptedTransport(*Array(5) { toolReply("wait", """{"seconds":1}""") })
        lateinit var agent: AgentLoop
        val stopper = EventSink { kind, _, _ -> if (kind == "ok") agent.stopRequested = true }
        agent = AgentLoop(spec(), FakePhone(state), LlmClient(transport, "u", { "k" }, {}), stopper)
        assertEquals(RunStatus.STOPPED, agent.run().status)
    }

    @Test
    fun `three failures in a row make a new plan`() {
        val phone = FakePhone(state).apply { failTaps = true }
        val transport = ScriptedTransport(
            textReply("""{"goals":["Open Settings"]}"""),
            toolReply("tap", """{"index":1}"""), toolReply("tap", """{"index":1}"""), toolReply("tap", """{"index":1}"""),
            textReply("""{"goals":["Use the app drawer"]}"""),
            toolReply("done", """{"success":false,"answer":"cannot"}"""),
        )
        val result = loop(spec(reasoning = true), transport, phone).run()
        assertEquals(RunStatus.FAILED, result.status)
        assertEquals(listOf("1. Open Settings", "1. Use the app drawer"), events.filter { it.first == "plan" }.map { it.second })
        assertEquals("p/m", transport.requests[4].second.getString("model"))
    }

    @Test
    fun `budget exhaustion fails the run`() {
        val transport = ScriptedTransport(HttpResult(403, """{"error":{"message":"Key limit exceeded"}}"""))
        val result = loop(spec(), transport).run()
        assertEquals(AgentLoop.Result(RunStatus.FAILED, LlmClient.BUDGET_MESSAGE, 0), result)
    }

    @Test
    fun `runs out of time`() {
        var now = 0L
        val transport = ScriptedTransport(*Array(5) { toolReply("wait", """{"seconds":1}""") })
        val result = loop(spec(), transport, clock = { now.also { now += 40_000 } }).run()
        assertEquals(RunStatus.FAILED, result.status)
        assertTrue(result.result.contains("time"))
    }

    @Test
    fun `vision sends the screenshot`() {
        val transport = ScriptedTransport(toolReply("done", """{"success":true,"answer":"ok"}"""))
        loop(spec(vision = true), transport).run()
        val content = transport.requests.first().second.getJSONArray("messages").getJSONObject(1).getJSONArray("content")
        assertEquals("data:image/jpeg;base64,SHOT", content.getJSONObject(1).getJSONObject("image_url").getString("url"))
    }
}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `gradle-test AgentLoopTest`
Expected: compile errors.

- [ ] **Step 3: Write `Planner.kt`**

```kotlin
package com.mobilerun.portal.agent

import org.json.JSONArray
import org.json.JSONObject

/** Short goals from the planner model; replans with what went wrong when the agent is stuck. */
class Planner(private val llm: LlmClient) {
    fun plan(spec: RunSpec, screen: Screen, trouble: List<String>): List<String> {
        val user = buildString {
            append("Task: ").append(spec.instruction).append("\n\n")
            if (trouble.isNotEmpty()) append("The previous plan got stuck. Recent actions:\n").append(trouble.joinToString("\n")).append("\n\n")
            append("Current screen:\n").append(ScreenReader.describe(screen))
        }
        val messages = JSONArray()
            .put(JSONObject().put("role", "system").put("content", spec.prompts.planner))
            .put(JSONObject().put("role", "user").put("content", user))
        val text = (llm.complete(spec.plannerModel, messages, null, 800) as? LlmReply.Text)?.text.orEmpty()
        return parseGoals(text)?.takeIf { it.isNotEmpty() } ?: listOf(spec.instruction)
    }

    companion object {
        fun parseGoals(text: String): List<String>? {
            val start = text.indexOf('{')
            val end = text.lastIndexOf('}')
            if (start < 0 || end <= start) return null
            val goals = runCatching { JSONObject(text.substring(start, end + 1)).getJSONArray("goals") }.getOrNull() ?: return null
            return (0 until goals.length()).mapNotNull { goals.optString(it).trim().takeIf(String::isNotEmpty) }.take(6)
        }
    }
}
```

- [ ] **Step 4: Write `AgentLoop.kt`**

```kotlin
package com.mobilerun.portal.agent

import org.json.JSONArray
import org.json.JSONObject

/** One task on this phone: (plan), then read the screen, ask for one action, do it, report; repeat. */
class AgentLoop(
    private val spec: RunSpec,
    private val phone: PhoneControl,
    private val llm: LlmClient,
    private val sink: EventSink,
    private val clock: () -> Long = System::currentTimeMillis,
) {
    data class Result(val status: RunStatus, val result: String, val steps: Int)

    @Volatile
    var stopRequested = false

    private val actions = Actions(phone)
    private val planner = Planner(llm)
    private val history = ArrayDeque<String>()

    fun run(): Result {
        val started = clock()
        var steps = 0
        var failuresInRow = 0
        var sameInRow = 0
        var lastKey = ""
        sink.event("phase", "Reading the screen", 0)
        var screen = observe() ?: return Result(RunStatus.FAILED, DispatcherPhoneControl.NO_SERVICE, 0)
        try {
            var goals = if (spec.reasoning) plan(screen, emptyList()) else emptyList()
            while (true) {
                if (stopRequested) return Result(RunStatus.STOPPED, "Stopped", steps)
                if (steps >= spec.maxSteps) return Result(RunStatus.FAILED, "Reached the step limit (${spec.maxSteps}) before finishing", steps)
                if (clock() - started > spec.timeLimitMs) return Result(RunStatus.FAILED, "Ran out of time", steps)
                sink.event("phase", "Thinking", steps)
                val reply = llm.complete(spec.executorModel, executorMessages(screen, goals), spec.prompts.tools)
                steps++
                val (key, outcome) = when (reply) {
                    is LlmReply.Text -> "text" to ActionOutcome(false, "The model answered without choosing an action: ${reply.text.take(200)}")
                    is LlmReply.Tool -> {
                        if (reply.thought.isNotBlank()) sink.event("think", reply.thought.take(500), steps)
                        val description = describe(reply.call)
                        sink.event("action", description, steps)
                        description to actions.run(reply.call, screen)
                    }
                }
                if (outcome.done) {
                    if (outcome.answer.isNotBlank()) sink.event("answer", outcome.answer, steps)
                    val status = if (outcome.success) RunStatus.SUCCEEDED else RunStatus.FAILED
                    return Result(status, outcome.answer.ifBlank { if (outcome.success) "Done" else "The agent gave up" }, steps)
                }
                sink.event(if (outcome.ok) "ok" else "error", outcome.text, steps)
                history.addLast("$steps. $key -> ${if (outcome.ok) "ok" else "failed"}: ${outcome.text}")
                while (history.size > HISTORY) history.removeFirst()
                failuresInRow = if (outcome.ok) 0 else failuresInRow + 1
                sameInRow = if (key == lastKey) sameInRow + 1 else 1
                lastKey = key
                if (failuresInRow >= STUCK || sameInRow >= STUCK) {
                    if (spec.reasoning) {
                        sink.event("phase", "Making a new plan", steps)
                        goals = plan(screen, history.toList())
                        failuresInRow = 0
                        sameInRow = 0
                    } else if (failuresInRow >= STUCK) {
                        return Result(RunStatus.FAILED, "Three actions in a row failed: ${outcome.text}", steps)
                    }
                }
                phone.sleep(SETTLE_MS)
                screen = observe() ?: screen
            }
        } catch (e: LlmError) {
            return Result(RunStatus.FAILED, e.message ?: "AI error", steps)
        }
    }

    private fun plan(screen: Screen, trouble: List<String>): List<String> {
        val goals = planner.plan(spec, screen, trouble)
        sink.event("plan", goals.mapIndexed { i, g -> "${i + 1}. $g" }.joinToString("\n"), null)
        return goals
    }

    private fun observe(): Screen? {
        val state = phone.readScreen() ?: return null
        val screen = ScreenReader.parse(state)
        return if (spec.vision) screen.copy(screenshotBase64 = phone.screenshot(VISION_SIDE, VISION_QUALITY)) else screen
    }

    private fun executorMessages(screen: Screen, goals: List<String>): JSONArray {
        val text = buildString {
            append("Task: ").append(spec.instruction).append('\n')
            if (goals.isNotEmpty()) append("\nPlan:\n").append(goals.mapIndexed { i, g -> "${i + 1}. $g" }.joinToString("\n")).append('\n')
            if (history.isNotEmpty()) append("\nRecent actions:\n").append(history.joinToString("\n")).append('\n')
            append("\nCurrent screen:\n").append(ScreenReader.describe(screen))
        }
        val content = JSONArray().put(JSONObject().put("type", "text").put("text", text))
        screen.screenshotBase64?.let {
            content.put(JSONObject().put("type", "image_url").put("image_url", JSONObject().put("url", "data:image/jpeg;base64,$it")))
        }
        return JSONArray()
            .put(JSONObject().put("role", "system").put("content", spec.prompts.executor))
            .put(JSONObject().put("role", "user").put("content", content))
    }

    private fun describe(call: ToolCall): String {
        val args = call.args.keys().asSequence().sorted().joinToString(" ") { "$it=${call.args.opt(it)}" }
        return if (args.isEmpty()) call.name else "${call.name} $args"
    }

    companion object {
        const val SETTLE_MS = 700L
        const val HISTORY = 8
        const val STUCK = 3
        const val VISION_SIDE = 960
        const val VISION_QUALITY = 60
    }
}
```
Note on the plan-events test: `plan` sends one event per plan, with text `"1. Open Settings"`. The first `STUCK` check happens after the third failed tap, so the replan uses `transport.requests[4]`, the fifth request.

- [ ] **Step 5: Run the tests**

Run: `gradle-test AgentLoopTest`
Expected: PASS (8 tests).

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -q -m "agent: planner and the run loop (step limit, time limit, stop, replan when stuck, budget errors)"
```

### Task 17: `Outbox` and `KeyVault`

**Files:**
- Create: `agent/Outbox.kt`, `agent/KeyVault.kt`
- Test: `agent/OutboxTest.kt`, `agent/KeyVaultTest.kt`

**Interfaces:**
- Produces:
  - `class Outbox(file: File, limit: Int = 5000)` with `add(message: JSONObject)`, `ack(uuid: String, seq: Int)`, `pending(): List<JSONObject>`, and `flush(send: (String) -> Boolean)`, which stops at the first failed send. Messages have the shape `{"method", "params": {"uuid", "seq", ...}}`.
  - `interface SecretBox { fun seal(plain: ByteArray): ByteArray; fun open(sealed: ByteArray): ByteArray }`
  - `class KeystoreSecretBox(alias: String = "fastautomate-agent-key") : SecretBox`
  - `class KeyVault(file: File, box: SecretBox)` with `save(key: String, hash: String)`, `key(): String?`, `hash(): String` ("" when empty) and `clear()`

- [ ] **Step 1: Write the failing tests**

`OutboxTest.kt`:
```kotlin
package com.mobilerun.portal.agent

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class OutboxTest {
    @get:Rule
    val tmp = TemporaryFolder()

    private fun msg(seq: Int) = JSONObject().put("method", "agent/event").put("params", JSONObject().put("uuid", "u1").put("seq", seq))

    @Test
    fun `reports stay until acked, in order, across restarts`() {
        val file = tmp.newFile("outbox.json")
        val outbox = Outbox(file)
        (1..3).forEach { outbox.add(msg(it)) }
        outbox.ack("u1", 2)
        assertEquals(listOf(1, 3), Outbox(file).pending().map { it.getJSONObject("params").getInt("seq") })
    }

    @Test
    fun `flush stops at the first failed send`() {
        val outbox = Outbox(tmp.newFile("o.json"))
        (1..3).forEach { outbox.add(msg(it)) }
        val sent = mutableListOf<Int>()
        outbox.flush { text -> val seq = JSONObject(text).getJSONObject("params").getInt("seq"); sent += seq; seq < 2 }
        assertEquals(listOf(1, 2), sent)
        assertEquals(3, outbox.pending().size)  // nothing is dropped by sending; only acks drop
    }

    @Test
    fun `a full outbox drops the oldest`() {
        val outbox = Outbox(tmp.newFile("o.json"), limit = 2)
        (1..3).forEach { outbox.add(msg(it)) }
        assertEquals(listOf(2, 3), outbox.pending().map { it.getJSONObject("params").getInt("seq") })
    }
}
```
`KeyVaultTest.kt`:
```kotlin
package com.mobilerun.portal.agent

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class KeyVaultTest {
    @get:Rule
    val tmp = TemporaryFolder()

    private val xor = object : SecretBox {
        override fun seal(plain: ByteArray) = plain.map { (it.toInt() xor 0x5A).toByte() }.toByteArray()
        override fun open(sealed: ByteArray) = seal(sealed)
    }

    @Test
    fun `the key is stored sealed and survives a restart`() {
        val file = tmp.newFile("key.json")
        KeyVault(file, xor).save("sk-or-v1-secret", "hash1")
        val vault = KeyVault(file, xor)
        assertEquals("sk-or-v1-secret", vault.key())
        assertEquals("hash1", vault.hash())
        assertFalse(file.readText().contains("sk-or-v1-secret"))
        vault.clear()
        assertNull(vault.key())
        assertEquals("", vault.hash())
    }
}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `gradle-test OutboxTest` and `gradle-test KeyVaultTest`
Expected: compile errors.

- [ ] **Step 3: Write `Outbox.kt`**

```kotlin
package com.mobilerun.portal.agent

import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/** Reports for the dashboard, kept on disk until it acks them, so nothing is lost while offline. */
class Outbox(private val file: File, private val limit: Int = 5000) {
    private val items = LinkedHashMap<String, JSONObject>()

    init {
        if (file.exists() && file.length() > 0) {
            runCatching { JSONArray(file.readText()) }.getOrNull()?.let { array ->
                for (i in 0 until array.length()) array.optJSONObject(i)?.let { items[key(it)] = it }
            }
        }
    }

    @Synchronized
    fun add(message: JSONObject) {
        items[key(message)] = message
        while (items.size > limit) items.remove(items.keys.first())
        save()
    }

    @Synchronized
    fun ack(uuid: String, seq: Int) {
        if (items.remove("$uuid#$seq") != null) save()
    }

    @Synchronized
    fun pending(): List<JSONObject> = items.values.toList()

    fun flush(send: (String) -> Boolean) {
        for (message in pending()) if (!send(message.toString())) return
    }

    private fun key(message: JSONObject): String {
        val params = message.getJSONObject("params")
        return "${params.getString("uuid")}#${params.getInt("seq")}"
    }

    private fun save() {
        val tmp = File(file.parentFile, file.name + ".tmp")
        tmp.writeText(JSONArray(items.values.toList()).toString())
        tmp.renameTo(file)
    }
}
```

- [ ] **Step 4: Write `KeyVault.kt`**

```kotlin
package com.mobilerun.portal.agent

import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import org.json.JSONObject
import java.io.File
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

interface SecretBox {
    fun seal(plain: ByteArray): ByteArray
    fun open(sealed: ByteArray): ByteArray
}

/** AES-GCM with a key that never leaves the Android Keystore. Output: 12-byte IV + ciphertext. */
class KeystoreSecretBox(private val alias: String = "fastautomate-agent-key") : SecretBox {
    private fun secretKey(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getEntry(alias, null) as? KeyStore.SecretKeyEntry)?.let { return it.secretKey }
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        generator.init(
            KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .build(),
        )
        return generator.generateKey()
    }

    override fun seal(plain: ByteArray): ByteArray {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding").apply { init(Cipher.ENCRYPT_MODE, secretKey()) }
        return cipher.iv + cipher.doFinal(plain)
    }

    override fun open(sealed: ByteArray): ByteArray {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, secretKey(), GCMParameterSpec(128, sealed, 0, 12))
        return cipher.doFinal(sealed, 12, sealed.size - 12)
    }
}

/** This phone's OpenRouter key (from the dashboard's agent/credentials), sealed on disk. */
class KeyVault(private val file: File, private val box: SecretBox) {
    @Synchronized
    fun save(key: String, hash: String) {
        val sealed = java.util.Base64.getEncoder().encodeToString(box.seal(key.toByteArray(Charsets.UTF_8)))
        file.writeText(JSONObject().put("hash", hash).put("sealed", sealed).toString())
    }

    @Synchronized
    fun key(): String? {
        val json = read() ?: return null
        return runCatching {
            String(box.open(java.util.Base64.getDecoder().decode(json.getString("sealed"))), Charsets.UTF_8)
        }.getOrNull()
    }

    @Synchronized
    fun hash(): String = read()?.optString("hash").orEmpty()

    @Synchronized
    fun clear() {
        file.delete()
    }

    private fun read(): JSONObject? =
        if (file.exists() && file.length() > 0) runCatching { JSONObject(file.readText()) }.getOrNull() else null
}
```

- [ ] **Step 5: Run the tests**

Run: `gradle-test OutboxTest` and `gradle-test KeyVaultTest`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -q -m "agent: outbox kept until acked, and the phone's key sealed with the Android Keystore"
```

### Task 18: `AgentHost` + wiring into the app

**Files:**
- Create: `agent/AgentHost.kt` (contains `AgentHost` and the `AgentRuntime` singleton)
- Modify:
  - `service/ActionDispatcher.kt` (the `when` in `dispatch`)
  - `service/HeadlessActionSupport.kt` (`isAllowed`)
  - `service/ReverseConnectionService.kt` (`buildHeaders`, `onOpen`)
  - `PortalApplication.kt` (`onCreate`)
- Test: `agent/AgentHostTest.kt`

**Interfaces:**
- Consumes: `AgentLoop` (Task 16), `LlmClient`/`LlmTransport` (Task 14), `Outbox`/`KeyVault` (Task 17), `DispatcherPhoneControl` (Task 13).
- Produces:
  - `class AgentHost(outbox, vault, phone: () -> PhoneControl?, transport, send: (String) -> Boolean, runner: (Runnable) -> Unit = { Thread(it, "FaAgent").start() }, clock: () -> Long = System::currentTimeMillis)` with:
    - `fun keyHash(): String`
    - `fun handle(method: String, params: JSONObject): ApiResponse`
    - `fun start(spec: RunSpec, announce: Boolean): ApiResponse`
    - `fun stop(uuid: String)`
    - `fun onConnected()`
    - `fun running(): String?` (the uuid)
    - `var listener: RunListener?`, where `interface RunListener { fun started(spec: RunSpec, origin: String); fun event(uuid: String, kind: String, text: String, steps: Int?); fun finished(uuid: String, status: RunStatus, result: String, steps: Int, shot: String?) }` is the hook RunStore uses in phase 3
  - `object AgentRuntime { fun init(context: Context); fun keyHash(): String; fun handle(method, params): ApiResponse; fun onConnected(); fun host(): AgentHost? }`
- Wire messages: `agent/started {uuid, seq: 0, ts, instruction, reasoning, max_steps}`, `agent/event {uuid, seq, ts, kind, text, steps?}`, `agent/finished {uuid, seq, ts, status, result, steps, shot?}`.

- [ ] **Step 1: Write the failing tests**

`AgentHostTest.kt`:
```kotlin
package com.mobilerun.portal.agent

import com.mobilerun.portal.api.ApiResponse
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class AgentHostTest {
    @get:Rule
    val tmp = TemporaryFolder()

    private val box = object : SecretBox {
        override fun seal(plain: ByteArray) = plain
        override fun open(sealed: ByteArray) = sealed
    }
    private val state = JSONObject("""{"a11y_tree":{},"phone_state":{},"device_context":{"screen_bounds":{"width":1080,"height":2400}}}""")
    private val sent = mutableListOf<JSONObject>()
    private val queued = mutableListOf<Runnable>()

    private fun host(transport: LlmTransport, phone: PhoneControl? = FakePhone(state), direct: Boolean = true) = AgentHost(
        outbox = Outbox(tmp.newFile()),
        vault = KeyVault(tmp.newFile(), box),
        phone = { phone },
        transport = transport,
        send = { sent += JSONObject(it); true },
        runner = { if (direct) it.run() else queued += it },
    )

    private fun runParams(uuid: String = "u1") = JSONObject(
        """{"uuid":"$uuid","instruction":"open settings","reasoning":false,"max_steps":5,"time_limit_s":900,"vision":false,
            "planner_model":"p/m","executor_model":"e/m","base_url":"https://openrouter.ai/api/v1",
            "prompts":{"version":"v","planner":"P","executor":"E","tools":[]}}""",
    )

    private fun credentials(host: AgentHost) =
        host.handle("agent/credentials", JSONObject().put("key", "sk-or-v1-phone").put("hash", "h1"))

    @Test
    fun `credentials are kept and a dashboard task runs and reports`() {
        val host = host(ScriptedTransport(toolReply("done", """{"success":true,"answer":"Settings is open"}""")))
        credentials(host)
        assertEquals("h1", host.keyHash())
        assertTrue(host.handle("agent/run", runParams()) !is ApiResponse.Error)
        val methods = sent.map { it.getString("method") }
        assertEquals("agent/finished", methods.last())
        assertTrue(methods.dropLast(1).all { it == "agent/event" })
        val finished = sent.last().getJSONObject("params")
        assertEquals(listOf("u1", "succeeded", "Settings is open", "SHOT"),
            listOf(finished.getString("uuid"), finished.getString("status"), finished.getString("result"), finished.getString("shot")))
        assertEquals((1..sent.size).toList(), sent.map { it.getJSONObject("params").getInt("seq") })
    }

    @Test
    fun `acks empty the outbox`() {
        val host = host(ScriptedTransport(toolReply("done", """{"success":true,"answer":"ok"}""")))
        credentials(host)
        host.handle("agent/run", runParams())
        sent.forEach { host.handle("agent/ack", JSONObject().put("uuid", "u1").put("seq", it.getJSONObject("params").getInt("seq"))) }
        sent.clear()
        host.onConnected()
        assertTrue(sent.isEmpty())
    }

    @Test
    fun `refused without a key, without accessibility, or while busy`() {
        val noKey = host(ScriptedTransport())
        assertTrue((noKey.handle("agent/run", runParams()) as ApiResponse.Error).message.contains("no AI key"))
        val noService = host(ScriptedTransport(), phone = null)
        credentials(noService)
        assertEquals(DispatcherPhoneControl.NO_SERVICE, (noService.handle("agent/run", runParams()) as ApiResponse.Error).message)
        val busy = host(ScriptedTransport(), direct = false)
        credentials(busy)
        busy.handle("agent/run", runParams("u1"))
        assertTrue((busy.handle("agent/run", runParams("u2")) as ApiResponse.Error).message.contains("already running"))
    }

    @Test
    fun `stop ends the run as stopped`() {
        val host = host(ScriptedTransport(*Array(3) { toolReply("wait", """{"seconds":1}""") }), direct = false)
        credentials(host)
        host.handle("agent/run", runParams())
        host.handle("agent/stop", JSONObject().put("uuid", "u1"))
        queued.single().run()
        assertEquals("stopped", sent.last().getJSONObject("params").getString("status"))
    }

    @Test
    fun `app-started runs announce themselves first`() {
        val host = host(ScriptedTransport(toolReply("done", """{"success":true,"answer":"ok"}""")))
        credentials(host)
        host.start(RunSpec.fromJson(runParams("u7")), announce = true)
        val first = sent.first()
        assertEquals("agent/started", first.getString("method"))
        assertEquals(0, first.getJSONObject("params").getInt("seq"))
        assertEquals("open settings", first.getJSONObject("params").getString("instruction"))
    }
}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `gradle-test AgentHostTest`
Expected: compile errors.

- [ ] **Step 3: Write `AgentHost.kt`**

```kotlin
package com.mobilerun.portal.agent

import android.content.Context
import com.mobilerun.portal.api.ApiResponse
import com.mobilerun.portal.service.MobilerunAccessibilityService
import com.mobilerun.portal.service.ReverseConnectionService
import org.json.JSONObject
import java.io.File
import java.time.Instant
import java.util.concurrent.atomic.AtomicInteger

interface RunListener {
    fun started(spec: RunSpec, origin: String)
    fun event(uuid: String, kind: String, text: String, steps: Int?)
    fun finished(uuid: String, status: RunStatus, result: String, steps: Int, shot: String?)
}

/** Runs one task at a time on this phone and reports it to the dashboard (numbered, kept until acked). */
class AgentHost(
    private val outbox: Outbox,
    private val vault: KeyVault,
    private val phone: () -> PhoneControl?,
    private val transport: LlmTransport,
    private val send: (String) -> Boolean,
    private val runner: (Runnable) -> Unit = { Thread(it, "FaAgent").start() },
    private val clock: () -> Long = System::currentTimeMillis,
) {
    @Volatile
    var listener: RunListener? = null

    @Volatile
    private var current: Pair<String, AgentLoop>? = null

    fun keyHash(): String = vault.hash()

    fun running(): String? = current?.first

    fun handle(method: String, params: JSONObject): ApiResponse = when (method) {
        "agent/credentials" -> {
            vault.save(params.getString("key"), params.getString("hash"))
            ApiResponse.RawObject(JSONObject().put("saved", true))
        }
        "agent/run" -> start(RunSpec.fromJson(params), announce = false)
        "agent/stop" -> {
            stop(params.optString("uuid"))
            ApiResponse.RawObject(JSONObject().put("stopping", true))
        }
        "agent/ack" -> {
            outbox.ack(params.optString("uuid"), params.optInt("seq", -1))
            ApiResponse.RawObject(JSONObject().put("ok", true))
        }
        else -> ApiResponse.Error("Unknown agent method $method")
    }

    fun start(spec: RunSpec, announce: Boolean): ApiResponse {
        val control = phone() ?: return ApiResponse.Error(DispatcherPhoneControl.NO_SERVICE)
        val key = vault.key() ?: return ApiResponse.Error(
            "This phone has no AI key yet. Add the OpenRouter management key in the dashboard Settings and keep the phone connected.",
        )
        val seq = AtomicInteger(0)
        val sink = EventSink { kind, text, steps ->
            val params = JSONObject().put("kind", kind).put("text", text)
            if (steps != null) params.put("steps", steps)
            post("agent/event", spec.uuid, seq.incrementAndGet(), params)
            listener?.event(spec.uuid, kind, text, steps)
        }
        val loop = AgentLoop(spec, control, LlmClient(transport, spec.baseUrl, { key }), sink, clock)
        synchronized(this) {
            if (current != null) return ApiResponse.Error("This phone is already running a task")
            current = spec.uuid to loop
        }
        listener?.started(spec, if (announce) "app" else "dashboard")
        if (announce) {
            post("agent/started", spec.uuid, 0, JSONObject().put("instruction", spec.instruction)
                .put("reasoning", spec.reasoning).put("max_steps", spec.maxSteps))
        }
        runner(Runnable { finish(spec, control, loop, seq) })
        return ApiResponse.RawObject(JSONObject().put("accepted", true))
    }

    fun stop(uuid: String) {
        current?.let { (id, loop) -> if (uuid.isEmpty() || id == uuid) loop.stopRequested = true }
    }

    fun onConnected() = outbox.flush(send)

    private fun finish(spec: RunSpec, control: PhoneControl, loop: AgentLoop, seq: AtomicInteger) {
        val result = try {
            loop.run()
        } catch (e: Exception) {
            AgentLoop.Result(RunStatus.FAILED, "The agent crashed: ${e.message}", 0)
        }
        val shot = runCatching { control.screenshot(SHOT_SIDE, SHOT_QUALITY) }.getOrNull()
        val params = JSONObject().put("status", result.status.wire).put("result", result.result).put("steps", result.steps)
        if (shot != null) params.put("shot", shot)
        post("agent/finished", spec.uuid, seq.incrementAndGet(), params)
        synchronized(this) { current = null }
        listener?.finished(spec.uuid, result.status, result.result, result.steps, shot)
    }

    private fun post(method: String, uuid: String, seq: Int, params: JSONObject) {
        params.put("uuid", uuid).put("seq", seq).put("ts", Instant.ofEpochMilli(clock()).toString())
        val message = JSONObject().put("method", method).put("params", params)
        outbox.add(message)
        send(message.toString())
    }

    companion object {
        const val SHOT_SIDE = 480
        const val SHOT_QUALITY = 60
    }
}

/** The app-wide agent: created once in PortalApplication, used by the dispatcher and the connection. */
object AgentRuntime {
    @Volatile
    private var host: AgentHost? = null

    @Synchronized
    fun init(context: Context) {
        if (host != null) return
        val dir = File(context.filesDir, "agent").apply { mkdirs() }
        host = AgentHost(
            outbox = Outbox(File(dir, "outbox.json")),
            vault = KeyVault(File(dir, "key.json"), KeystoreSecretBox()),
            phone = {
                MobilerunAccessibilityService.getInstance()?.let { service ->
                    DispatcherPhoneControl { service.getActionDispatcher() }
                }
            },
            transport = OkHttpTransport(),
            send = { text -> ReverseConnectionService.getInstance()?.sendText(text) ?: false },
        )
    }

    fun host(): AgentHost? = host

    fun keyHash(): String = host?.keyHash().orEmpty()

    fun handle(method: String, params: JSONObject): ApiResponse =
        host?.handle(method, params) ?: ApiResponse.Error("The agent is not ready yet")

    fun onConnected() {
        host?.onConnected()
    }
}
```

- [ ] **Step 4: Run the tests**

Run: `gradle-test AgentHostTest`
Expected: PASS (5 tests). (`MobilerunAccessibilityService.getActionDispatcher()` is public, and `ApiResponse.RawObject` goes over the reverse socket as `{"status": "success", "result": {...}}`.)

- [ ] **Step 5: Wire it into the app**

- `service/ActionDispatcher.kt`: inside `when (val method = ...)`, add as the first branch:
```kotlin
            // FastAutomate v2: the agent runs on this phone
            "agent/run", "agent/stop", "agent/credentials", "agent/ack" ->
                com.mobilerun.portal.agent.AgentRuntime.handle(method, params)
```
- `service/HeadlessActionSupport.kt`: in `isAllowed`, change `return normalizedMethod == "stream/start" ||` to `return normalizedMethod.startsWith("agent/") || normalizedMethod == "stream/start" ||`. This lets credentials and acks arrive even while accessibility is off; `agent/run` then answers "Turn on FastAutomate v2 in Accessibility".
- `service/ReverseConnectionService.kt`, in `buildHeaders()` after `headers["X-Android-Version"] = ...`:
```kotlin
        headers["X-Agent-Key-Hash"] = com.mobilerun.portal.agent.AgentRuntime.keyHash()
```
  and inside `override fun onOpen(handshakedata: ServerHandshake?)` right after `ConnectionStateManager.setState(ConnectionState.CONNECTED)`:
```kotlin
                    com.mobilerun.portal.agent.AgentRuntime.onConnected() // resend reports the dashboard has not acked
```
- `PortalApplication.kt`: in `onCreate()`, right after `super.onCreate()`:
```kotlin
        com.mobilerun.portal.agent.AgentRuntime.init(this)
```

- [ ] **Step 6: Run all unit tests and build**

Run: `./gradlew -q testDebugUnitTest && ./build-fa.sh`
Expected: all tests pass; `dist/fa-portal-v2.apk` is built.

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -q -m "agent host: one run at a time, numbered reports kept until acked, wired into the dispatcher and the dashboard connection"
```

### Task 19: Phase 2 end to end on the redroid

**Files:** none (deploy and verify).

- [ ] **Step 1: Ship the app inside the dashboard, deploy, install on the redroid**

```bash
cp ~/claude_projects/fa-portal-v2/dist/fa-portal-v2.apk ~/claude_projects/mobile-rpa-v2/mobile_rpa/fa-portal-v2.apk
cd ~/claude_projects/mobile-rpa-v2 && ./deploy/deploy.sh
adb -s 192.168.100.23:5555 install -r -g ~/claude_projects/fa-portal-v2/dist/fa-portal-v2.apk
```
Expected: `active`, `http 200`, `Success`. The app reconnects by itself within about 15 s.

- [ ] **Step 2: Put the management key into v2 Settings** (the key is used only inside this one command; the user pasted it in chat)

```bash
C=/tmp/claude-1000/-home-arrow/573c1627-48ac-4d09-93fa-91d5ddaa417a/scratchpad/cj2
rtk proxy curl -s -c $C -H 'content-type: application/json' -d '{"password":"connect"}' https://digimate.fastautomate.com/mobile2/api/login >/dev/null
rtk proxy curl -s -b $C -X PUT -H 'content-type: application/json' https://digimate.fastautomate.com/mobile2/api/settings \
  -d '{"management_key":"<the management key from chat>"}' | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['management_key_set'], d['management_key_hint'])"
sleep 10
rtk proxy curl -s -b $C https://digimate.fastautomate.com/mobile2/api/state | python3 -c "import json,sys; [print(p['name'], p['status'], bool(p['key_hash']), p['note']) for p in json.load(sys.stdin)['phones']]"
```
Expected: `True ...xxxx`, then the redroid phone listed as `online True` with an empty note. The OpenRouter key list (`GET /api/v1/keys` with the management key) shows `fastautomate-v2-<id>-redroid12_x86_64`.

- [ ] **Step 3: Run a task from the dashboard**

```bash
PID=$(rtk proxy curl -s -b $C https://digimate.fastautomate.com/mobile2/api/state | python3 -c "import json,sys; print(json.load(sys.stdin)['phones'][0]['id'])")
TID=$(rtk proxy curl -s -b $C -X POST -H 'content-type: application/json' https://digimate.fastautomate.com/mobile2/api/tasks \
  -d "{\"prompt\":\"open the settings app\",\"runs\":[{\"phone_id\":$PID,\"instruction\":\"open the settings app\"}]}" | python3 -c "import json,sys; print(json.load(sys.stdin).get('id'))")
sleep 45
rtk proxy curl -s -b $C https://digimate.fastautomate.com/mobile2/api/tasks/$TID | python3 -c "
import json,sys; t=json.load(sys.stdin); r=t['runs'][0]
print(r['status'], '|', r['result']); [print(' ', e['kind'], e['text'][:90]) for e in r['events']]"
```
Expected with OpenRouter credit: `succeeded | ...`, and a step log with `plan`, `action`, `ok` and `done`. Expected while the account is empty ($0.01): `failed | The OpenRouter account is out of credit`. That proves the whole path (dashboard, phone, OpenRouter with the phone's own key), and the credit guard, which checks at under $0.05, blocks the task before sending it. Either way, `GET /api/runs/<run id>/shot.jpg` returns a JPEG when the run got that far.

- [ ] **Step 4: Check that nothing streamed to the server during the run**

```bash
ssh -i ~/.ssh/id_root root@192.168.100.20 "pct exec 124 -- journalctl -u mobile-rpa-v2 --since '-3 min' --no-pager | grep -c 'screen.jpg'"
```
Expected: `0` while the Phones page is not open in any browser. Thumbnails are the only screenshots the dashboard requests.

- [ ] **Step 5: A dashboard restart mid-run does not lose the run** (needs credit)

Start a longer task ("open settings, then open About phone and tell me the Android version"). While it runs, run `ssh ... "pct exec 124 -- systemctl restart mobile-rpa-v2"`. Expected: the run continues on the phone; after the app reconnects, the missing steps arrive, and the run ends `succeeded` with every step once.

---
# Phase 3: the app's own runs, spending, pause

### Task 20: The app's Run button runs on the phone (local history for the app's screens)

**Files:**
- Create: `agent/RunStore.kt`, `agent/LocalTasks.kt`, `app/src/main/assets/agent_defaults.json` (generated)
- Modify:
  - `agent/AgentHost.kt`: constructor params `defaultsFile: File? = null`, `bundledDefaults: () -> String? = { null }`; save defaults on `agent/run`; `fun startLocal(instruction: String, reasoning: Boolean?, maxSteps: Int?): ApiResponse`; `AgentRuntime` wires these and the `RunStore`
  - `taskprompt/PortalCloudClient.kt`: task methods delegate to `LocalTasks`; `parseTaskDetails` and `parseTaskStatus` become `internal`
- Test: `agent/RunStoreTest.kt`, `agent/LocalTasksTest.kt`, one more test in `agent/AgentHostTest.kt`

**Interfaces:**
- Consumes: `RunListener`, `AgentHost.start` (Task 18); `PortalCloudClient.parseTaskDetails`, `parseTaskStatus`, `parseTaskHistoryPage`, `parseTaskTrajectory` (existing parsers of the dashboard's v1 REST shapes)
- Produces:
  - `data class RunEvent(kind, text, ts)` and `data class RunRecord(uuid, instruction, origin, status, createdAt, finishedAt, steps, result, events: List<RunEvent>)`
  - `class RunStore(dir: File, clock: () -> Long = System::currentTimeMillis, keep: Int = 100) : RunListener` with `get(uuid)`, `page(page, size): Pair<List<RunRecord>, Int>` (newest first)
  - `object LocalTasks` with:
    - `const val ENABLED = true`
    - `launch(draft, callback)`, `status(taskId, callback)`, `details(taskId, callback)`
    - `list(page, pageSize, callback)`, `screenshots(taskId, callback)`, `trajectory(taskId, callback)`
    - `cancel(taskId, callback)`, `models(callback)`, `balance(callback)`
    - pure builders `taskJson(r)`, `statusJson(r)`, `historyJson(records, page, size, total)`, `trajectoryJson(r)`
  - `AgentRuntime.store(): RunStore?`
  - Status mapping, as v1's dashboard did: running → `running`, succeeded → `completed`, failed → `failed`, stopped → `cancelled`

- [ ] **Step 1: Generate the bundled defaults from the dashboard (single source of the instructions)**

```bash
mkdir -p ~/claude_projects/fa-portal-v2/app/src/main/assets
cd ~/claude_projects/mobile-rpa-v2 && .venv/bin/python -c "from mobile_rpa.agent_prompts import defaults_json; print(defaults_json())" \
  > ~/claude_projects/fa-portal-v2/app/src/main/assets/agent_defaults.json
python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/claude_projects/fa-portal-v2/app/src/main/assets/agent_defaults.json'))); print(d['max_steps'], len(d['prompts']['tools']))"
```
Expected: `30 10`.

- [ ] **Step 2: Write the failing tests**

`RunStoreTest.kt`:
```kotlin
package com.mobilerun.portal.agent

import org.json.JSONArray
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class RunStoreTest {
    @get:Rule
    val tmp = TemporaryFolder()

    private fun spec(uuid: String) = RunSpec(uuid, "open settings", false, 5, 60_000, false, "p", "e", "u",
        Prompts("v", "P", "E", JSONArray()))

    @Test
    fun `records runs with their steps and keeps the newest`() {
        val dir = tmp.newFolder()
        val store = RunStore(dir, clock = { 1_790_000_000_000 }, keep = 2)
        listOf("a", "b", "c").forEach { store.started(spec(it), "app") }
        store.event("c", "action", "Tap Settings", 1)
        store.finished("c", RunStatus.SUCCEEDED, "Settings is open", 1, null)
        val again = RunStore(dir)
        val (items, total) = again.page(1, 10)
        assertEquals(listOf("c", "b"), items.map { it.uuid })
        assertEquals(2, total)
        val c = again.get("c")!!
        assertEquals(listOf("succeeded", "Settings is open", "1"), listOf(c.status, c.result, c.steps.toString()))
        assertEquals("Tap Settings", c.events.single().text)
    }
}
```
`LocalTasksTest.kt`:
```kotlin
package com.mobilerun.portal.agent

import com.mobilerun.portal.taskprompt.PortalCloudClient
import org.junit.Assert.assertEquals
import org.junit.Test

class LocalTasksTest {
    private val record = RunRecord(
        uuid = "u1", instruction = "open settings", origin = "app", status = "succeeded",
        createdAt = "2026-09-25T10:00:00Z", finishedAt = "2026-09-25T10:01:00Z", steps = 2, result = "Settings is open",
        events = listOf(RunEvent("plan", "1. Open Settings", "t1"), RunEvent("action", "tap index=3", "t2"), RunEvent("phase", "Thinking", "t3")),
    )

    @Test
    fun `the app's parsers read the local task`() {
        val details = PortalCloudClient.parseTaskDetails(LocalTasks.taskJson(record).toString(), "u1")!!
        assertEquals(listOf("u1", "completed", "open settings"), listOf(details.taskId, details.status, details.prompt))
        assertEquals("completed", PortalCloudClient.parseTaskStatus(LocalTasks.statusJson(record).toString()))
    }

    @Test
    fun `history and trajectory use the dashboard's shapes`() {
        val page = PortalCloudClient.parseTaskHistoryPage(LocalTasks.historyJson(listOf(record), 1, 20, 1).toString())!!
        assertEquals(listOf("u1"), page.items.map { it.taskId })
        val trajectory = PortalCloudClient.parseTaskTrajectory(LocalTasks.trajectoryJson(record).toString())!!
        assertEquals(listOf("ManagerPlanDetailsEvent", "ExecutorActionEvent"), trajectory.events.map { it.event })  // phases hidden
    }
}
```
Add to `AgentHostTest.kt`:
```kotlin
    @Test
    fun `the app's own run uses the last settings from the dashboard`() {
        val dir = tmp.newFolder()
        val host = AgentHost(
            outbox = Outbox(tmp.newFile()), vault = KeyVault(tmp.newFile(), box), phone = { FakePhone(state) },
            transport = ScriptedTransport(toolReply("done", """{"success":true,"answer":"ok"}"""),
                toolReply("done", """{"success":true,"answer":"ok"}""")),
            send = { sent += JSONObject(it); true }, runner = { it.run() },
            defaultsFile = java.io.File(dir, "defaults.json"), bundledDefaults = { null },
        )
        credentials(host)
        host.handle("agent/run", runParams())  // saves the dashboard's settings
        sent.clear()
        assertTrue(host.startLocal("check the weather", reasoning = false, maxSteps = 7) !is ApiResponse.Error)
        val started = sent.first().getJSONObject("params")
        assertEquals(listOf("agent/started", "check the weather", "7"),
            listOf(sent.first().getString("method"), started.getString("instruction"), started.getInt("max_steps").toString()))
    }
```

- [ ] **Step 3: Run them to verify they fail**

Run: `gradle-test RunStoreTest`, `gradle-test LocalTasksTest` and `gradle-test AgentHostTest`
Expected: compile errors.

- [ ] **Step 4: Write `RunStore.kt`**

```kotlin
package com.mobilerun.portal.agent

import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.time.Instant

data class RunEvent(val kind: String, val text: String, val ts: String)

data class RunRecord(
    val uuid: String,
    val instruction: String,
    val origin: String,
    val status: String,
    val createdAt: String,
    val finishedAt: String?,
    val steps: Int,
    val result: String,
    val events: List<RunEvent>,
)

/** The phone's own history of runs (both origins), for the app's task screens. */
class RunStore(
    private val dir: File,
    private val clock: () -> Long = System::currentTimeMillis,
    private val keep: Int = 100,
) : RunListener {
    private val file = File(dir, "runs.json")
    private val runs = LinkedHashMap<String, RunRecord>()

    init {
        dir.mkdirs()
        if (file.exists()) {
            runCatching { JSONArray(file.readText()) }.getOrNull()?.let { array ->
                for (i in 0 until array.length()) array.optJSONObject(i)?.let { runs[it.getString("uuid")] = fromJson(it) }
            }
        }
    }

    @Synchronized
    override fun started(spec: RunSpec, origin: String) {
        runs[spec.uuid] = RunRecord(spec.uuid, spec.instruction, origin, "running", now(), null, 0, "", emptyList())
        while (runs.size > keep) runs.remove(runs.keys.first())
        save()
    }

    @Synchronized
    override fun event(uuid: String, kind: String, text: String, steps: Int?) {
        val r = runs[uuid] ?: return
        runs[uuid] = r.copy(steps = steps ?: r.steps, events = r.events + RunEvent(kind, text, now()))
        save()
    }

    @Synchronized
    override fun finished(uuid: String, status: RunStatus, result: String, steps: Int, shot: String?) {
        val r = runs[uuid] ?: return
        runs[uuid] = r.copy(status = status.wire, result = result, steps = steps, finishedAt = now())
        save()
    }

    @Synchronized
    fun get(uuid: String): RunRecord? = runs[uuid]

    @Synchronized
    fun page(page: Int, size: Int): Pair<List<RunRecord>, Int> {
        val newest = runs.values.reversed()
        return newest.drop((page.coerceAtLeast(1) - 1) * size).take(size) to newest.size
    }

    private fun now() = Instant.ofEpochMilli(clock()).toString()

    private fun save() {
        val array = JSONArray()
        runs.values.forEach { r ->
            array.put(
                JSONObject().put("uuid", r.uuid).put("instruction", r.instruction).put("origin", r.origin)
                    .put("status", r.status).put("createdAt", r.createdAt).put("finishedAt", r.finishedAt ?: JSONObject.NULL)
                    .put("steps", r.steps).put("result", r.result)
                    .put("events", JSONArray(r.events.map { JSONObject().put("kind", it.kind).put("text", it.text).put("ts", it.ts) })),
            )
        }
        val tmp = File(dir, "runs.json.tmp")
        tmp.writeText(array.toString())
        tmp.renameTo(file)
    }

    private fun fromJson(j: JSONObject): RunRecord {
        val events = j.optJSONArray("events") ?: JSONArray()
        return RunRecord(
            uuid = j.getString("uuid"),
            instruction = j.getString("instruction"),
            origin = j.optString("origin"),
            status = j.optString("status"),
            createdAt = j.optString("createdAt"),
            finishedAt = if (j.isNull("finishedAt")) null else j.optString("finishedAt"),
            steps = j.optInt("steps"),
            result = j.optString("result"),
            events = (0 until events.length()).map { i ->
                events.getJSONObject(i).let { e -> RunEvent(e.getString("kind"), e.getString("text"), e.optString("ts")) }
            },
        )
    }
}
```

- [ ] **Step 5: Write `LocalTasks.kt`**

```kotlin
package com.mobilerun.portal.agent

import com.mobilerun.portal.api.ApiResponse
import com.mobilerun.portal.taskprompt.PortalBalanceResult
import com.mobilerun.portal.taskprompt.PortalCloudClient
import com.mobilerun.portal.taskprompt.PortalModelOption
import com.mobilerun.portal.taskprompt.PortalModelsLoadResult
import com.mobilerun.portal.taskprompt.PortalTaskCancelResult
import com.mobilerun.portal.taskprompt.PortalTaskDetailsResult
import com.mobilerun.portal.taskprompt.PortalTaskDraft
import com.mobilerun.portal.taskprompt.PortalTaskHistoryResult
import com.mobilerun.portal.taskprompt.PortalTaskLaunchResult
import com.mobilerun.portal.taskprompt.PortalTaskLaunchSuccess
import com.mobilerun.portal.taskprompt.PortalTaskScreenshotResult
import com.mobilerun.portal.taskprompt.PortalTaskScreenshotSet
import com.mobilerun.portal.taskprompt.PortalTaskStatusResult
import com.mobilerun.portal.taskprompt.PortalTaskStatusSuccess
import com.mobilerun.portal.taskprompt.PortalTaskTrajectoryResult
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.Executors

/** FastAutomate v2: the app's task screens read the phone's own runs instead of a server. */
object LocalTasks {
    const val ENABLED = true
    private val worker = Executors.newSingleThreadExecutor()
    private val STATUS = mapOf("running" to "running", "succeeded" to "completed", "failed" to "failed", "stopped" to "cancelled")
    private val HIDDEN = setOf("phase", "status")

    private fun host() = AgentRuntime.host()
    private fun store() = AgentRuntime.store()
    private fun later(block: () -> Unit) = worker.execute(block)

    fun launch(draft: PortalTaskDraft, callback: (PortalTaskLaunchResult) -> Unit) = later {
        val response = host()?.startLocal(draft.prompt, draft.settings.reasoning, draft.settings.maxSteps)
            ?: ApiResponse.Error("The agent is not ready yet")
        callback(
            if (response is ApiResponse.Error) {
                PortalTaskLaunchResult.Error(response.message)
            } else {
                PortalTaskLaunchResult.Success(PortalTaskLaunchSuccess(host()?.running().orEmpty()))
            },
        )
    }

    fun status(taskId: String, callback: (PortalTaskStatusResult) -> Unit) = later {
        val record = store()?.get(taskId)
        callback(
            if (record == null) {
                PortalTaskStatusResult.Error("No such task")
            } else {
                PortalTaskStatusResult.Success(PortalTaskStatusSuccess(STATUS[record.status] ?: "failed"))
            },
        )
    }

    fun details(taskId: String, callback: (PortalTaskDetailsResult) -> Unit) = later {
        val details = store()?.get(taskId)?.let { PortalCloudClient.parseTaskDetails(taskJson(it).toString(), taskId) }
        callback(if (details == null) PortalTaskDetailsResult.Error("No such task") else PortalTaskDetailsResult.Success(details))
    }

    fun list(page: Int, pageSize: Int, callback: (PortalTaskHistoryResult) -> Unit) = later {
        val (items, total) = store()?.page(page, pageSize) ?: (emptyList<RunRecord>() to 0)
        val parsed = PortalCloudClient.parseTaskHistoryPage(historyJson(items, page, pageSize, total).toString())
        callback(if (parsed == null) PortalTaskHistoryResult.Error("History unavailable") else PortalTaskHistoryResult.Success(parsed))
    }

    fun screenshots(taskId: String, callback: (PortalTaskScreenshotResult) -> Unit) = later {
        callback(PortalTaskScreenshotResult.Success(PortalTaskScreenshotSet(emptyList())))
    }

    fun trajectory(taskId: String, callback: (PortalTaskTrajectoryResult) -> Unit) = later {
        val parsed = store()?.get(taskId)?.let { PortalCloudClient.parseTaskTrajectory(trajectoryJson(it).toString()) }
        callback(if (parsed == null) PortalTaskTrajectoryResult.Error("No such task") else PortalTaskTrajectoryResult.Success(parsed))
    }

    fun cancel(taskId: String, callback: (PortalTaskCancelResult) -> Unit) = later {
        if (host()?.running() == taskId) {
            host()?.stop(taskId)
            callback(PortalTaskCancelResult.Success)
        } else {
            callback(PortalTaskCancelResult.AlreadyFinished)
        }
    }

    fun models(callback: (PortalModelsLoadResult) -> Unit) = later {
        callback(PortalModelsLoadResult(listOf(PortalModelOption("fastautomate/agent", "Agent")), null, true))
    }

    fun balance(callback: (PortalBalanceResult) -> Unit) = later {
        callback(PortalBalanceResult.Unavailable("Spending is shown on the dashboard"))
    }

    // ---- the dashboard's v1 REST shapes, which the app's parsers already read ------------------
    fun taskJson(r: RunRecord): JSONObject {
        val status = STATUS[r.status] ?: "failed"
        val ended = status != "running"
        return JSONObject().put(
            "task",
            JSONObject().put("id", r.uuid).put("status", status).put("task", r.instruction).put("createdAt", r.createdAt)
                .put("finishedAt", r.finishedAt ?: JSONObject.NULL).put("steps", r.steps)
                .put("succeeded", if (ended) r.status == "succeeded" else JSONObject.NULL)
                .put("output", if (r.result.isEmpty()) JSONObject.NULL else r.result).put("llmModel", "fastautomate/agent"),
        )
    }

    fun statusJson(r: RunRecord): JSONObject = JSONObject().put("status", STATUS[r.status] ?: "failed")

    fun historyJson(records: List<RunRecord>, page: Int, size: Int, total: Int): JSONObject {
        val pages = maxOf(1, (total + size - 1) / size)
        return JSONObject()
            .put("items", JSONArray(records.map { taskJson(it).getJSONObject("task") }))
            .put(
                "pagination",
                JSONObject().put("page", page).put("pageSize", size).put("total", total).put("pages", pages)
                    .put("hasNext", page < pages).put("hasPrev", page > 1),
            )
    }

    fun trajectoryJson(r: RunRecord): JSONObject {
        val events = JSONArray()
        r.events.filter { it.kind !in HIDDEN }.forEach { e ->
            val (name, data) = when (e.kind) {
                "plan" -> "ManagerPlanDetailsEvent" to JSONObject().put("plan", e.text)
                "answer" -> "ManagerPlanDetailsEvent" to JSONObject().put("answer", e.text)
                "action" -> "ExecutorActionEvent" to JSONObject().put("description", e.text)
                "ok" -> "ExecutorActionResultEvent" to JSONObject().put("success", true).put("summary", e.text)
                "error" -> "ExecutorActionResultEvent" to JSONObject().put("success", false).put("summary", e.text)
                "think" -> "FastAgentResponseEvent" to JSONObject().put("thought", e.text)
                else -> "InfoEvent" to JSONObject().put("message", e.text)
            }
            events.put(JSONObject().put("event", name).put("data", data).put("timestamp", e.ts))
        }
        return JSONObject().put("trajectory", events)
    }
}
```
In `PortalCloudClient`'s companion, change `private fun parseTaskStatus` and `private fun parseTaskDetails` to `internal fun`.

- [ ] **Step 6: `AgentHost`: defaults and the app's own runs**

Add the constructor parameters `private val defaultsFile: File? = null` and `private val bundledDefaults: () -> String? = { null }` (and `import java.io.File` if missing). Change the `"agent/run"` branch of `handle` to:
```kotlin
        "agent/run" -> {
            defaultsFile?.writeText(JSONObject(params.toString()).apply { remove("uuid"); remove("instruction") }.toString())
            start(RunSpec.fromJson(params), announce = false)
        }
```
Add:
```kotlin
    /** The app's own Run button: the last settings the dashboard sent (or the bundled ones). */
    fun startLocal(instruction: String, reasoning: Boolean?, maxSteps: Int?): ApiResponse {
        val saved = defaultsFile?.takeIf { it.exists() }?.readText() ?: bundledDefaults()
            ?: return ApiResponse.Error("Connect this phone to the dashboard once first")
        val json = JSONObject(saved).put("uuid", java.util.UUID.randomUUID().toString()).put("instruction", instruction.trim())
        if (reasoning != null) json.put("reasoning", reasoning)
        if (maxSteps != null) json.put("max_steps", maxSteps)
        return start(RunSpec.fromJson(json), announce = true)
    }
```
In `AgentRuntime`: add `@Volatile private var store: RunStore? = null` and `fun store(): RunStore? = store`. In `init`, before creating the host, set `store = RunStore(File(dir, "runs"))`. Pass `defaultsFile = File(dir, "defaults.json")` and `bundledDefaults = { runCatching { context.assets.open("agent_defaults.json").bufferedReader().readText() }.getOrNull() }` to `AgentHost(...)`, then set `host?.listener = store`.

- [ ] **Step 7: Point `PortalCloudClient`'s task methods at `LocalTasks`**

Add `import com.mobilerun.portal.agent.LocalTasks`, then make this the first line of each method body:
- `launchTask`: `if (LocalTasks.ENABLED) return LocalTasks.launch(draft, callback)`
- `getTaskStatus`: `if (LocalTasks.ENABLED) return LocalTasks.status(taskId, callback)`
- `getTask`: `if (LocalTasks.ENABLED) return LocalTasks.details(taskId, callback)`
- `listTasks`: `if (LocalTasks.ENABLED) return LocalTasks.list(page, pageSize, callback)`
- `getTaskScreenshots`: `if (LocalTasks.ENABLED) return LocalTasks.screenshots(taskId, callback)`
- `getTaskTrajectory`: `if (LocalTasks.ENABLED) return LocalTasks.trajectory(taskId, callback)`
- `cancelTask`: `if (LocalTasks.ENABLED) return LocalTasks.cancel(taskId, callback)`
- `loadModels`: `if (LocalTasks.ENABLED) return LocalTasks.models(callback)`
- `loadBalance`: `if (LocalTasks.ENABLED) return LocalTasks.balance(callback)`

`PortalTaskLaunchCoordinator` may confirm a launch by listing tasks. `RunStore.started` records the run before `startLocal` returns, so `LocalTasks.list` already contains it.

- [ ] **Step 8: Run all unit tests, build, check on the redroid**

Run: `./gradlew -q testDebugUnitTest && ./build-fa.sh`, then install on the redroid (Task 19, Step 1). In the app, type "open the settings app" and press Run Task. Expected: the task card shows it running, then a result (or the budget or credit message). The dashboard's History lists it as a new task.

- [ ] **Step 9: Commit**

```bash
git add -A && git commit -q -m "app's own runs happen on the phone: local history for the task screens, last dashboard settings reused"
```

### Task 21: Dashboard: drop the old app REST, pause/resume and spend per phone

**Files:**
- Modify:
  - `mobile_rpa/appconnect.py`: delete the `# ---- the app's own task screen` section (`device_phone`, `own_task`, `/v1/models`, every `/v1/tasks...` route) and the now unused `TASK_STATUS`, `_app_task`, `_trajectory_event`
  - `mobile_rpa/phones.py`: `spend` dict; `view()` adds `paused` and `spend`
  - `mobile_rpa/app.py`: pause/resume routes, spend refresh, paused check in `launch`, updated `register` call
  - `mobile_rpa/static/app.js`, `mobile_rpa/static/index.html`
- Test: `tests/test_app.py`; in `tests/test_appconnect.py` delete `test_app_prompt_runs_a_task_on_its_own_phone` and `test_trajectory_uses_mobilerun_event_names`; `tests/test_static.py`

**Interfaces:**
- Produces:
  - `POST /api/phones/{id}/pause` and `POST /api/phones/{id}/resume` return `{"ok": true}`
  - phone view fields `paused: bool`, `spend: float | null` (today's USD)
  - `launch` answers 409 "`<name>` is paused" for a paused phone
  - `appconnect.register(app, *, env, db, phones, devices, on_phone_ready) -> None`

- [ ] **Step 1: Write the failing tests** (append to `tests/test_app.py`)

```python
def test_pause_and_resume_a_phone(client, monkeypatch):
    login(client)
    fake = ready(client, monkeypatch)
    with join(client) as ws:
        phone = AgentPhone(ws)
        p = keyed_phone(client)
        assert client.post(f"/api/phones/{p['id']}/pause").json() == {"ok": True}
        assert the_phone(client)["paused"] is True
        r = client.post("/api/tasks", json={"prompt": "x", "runs": [{"phone_id": p["id"], "instruction": "x"}]})
        assert r.status_code == 409 and "paused" in r.json()["detail"]
        client.post(f"/api/phones/{p['id']}/resume")
        assert the_phone(client)["paused"] is False
        assert ("disabled", "hash-1", True) in fake.log and ("disabled", "hash-1", False) in fake.log
        phone.stop.set()


def test_spend_shows_on_the_card_after_a_run(client, monkeypatch):
    login(client)
    ready(client, monkeypatch)
    with join(client) as ws:
        phone = AgentPhone(ws)
        run_task(client, keyed_phone(client))
        assert wait_for(lambda: the_phone(client)["spend"] == 0.12)
        phone.stop.set()


def test_budget_failure_shows_on_the_card(client, monkeypatch):
    login(client)
    ready(client, monkeypatch)
    with join(client) as ws:
        phone = AgentPhone(ws)
        run_task(client, keyed_phone(client), "budget test")
        assert "daily AI budget" in wait_for(lambda: the_phone(client)["note"])
        phone.stop.set()


def test_old_app_task_api_is_gone(client):
    assert client.get("/v1/models").status_code in (404, 405)
```
Append to `tests/test_static.py`:
```python
def test_v2_pause_and_spend_ui():
    js = (STATIC / "app.js").read_text()
    html = (STATIC / "index.html").read_text()
    assert 'id="v-pause"' in html and "/pause" in js and "/resume" in js
    assert "today" in js  # spend chip
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_app.py tests/test_static.py -q`
Expected: FAIL (`KeyError: 'paused'`, `v-pause` missing).

- [ ] **Step 3: Implement**

`phones.py`: in `__init__` add `self.spend: dict[str, float] = {}  # serial -> today's USD`. Make `view` return:
```python
        return {**phone, "link": "app", "status": self.status(serial), "run_id": self.busy.get(serial),
                "note": self.notes.get(serial, ""), "paused": bool(phone.get("paused")), "spend": self.spend.get(serial)}
```
In `remove`, add `self.spend.pop(serial, None)`.

`app.py`:
- `from .openrouter_keys import KeyApiError`
- After `press_key`:
```python
    @app.post("/api/phones/{phone_id}/pause")
    async def pause_phone(phone_id: int):
        return await set_paused(phone_or_404(phone_id), True)

    @app.post("/api/phones/{phone_id}/resume")
    async def resume_phone(phone_id: int):
        return await set_paused(phone_or_404(phone_id), False)

    async def set_paused(phone: dict, paused: bool) -> dict:
        try:
            await phone_keys.pause(phone, paused)
        except KeyApiError as exc:
            raise HTTPException(502, str(exc)) from exc
        phones.publish()
        return {"ok": True}
```
- In `launch`, inside `for phone in picked.values():`, before the key check:
```python
            if phone["paused"]:
                raise HTTPException(409, f"{phone['name']} is paused. Resume it to run tasks.")
```
- Spend (after `refresh_credit`):
```python
    async def refresh_spend(phone: dict | None = None) -> None:
        for p in [phone] if phone else db.phones():
            value = await phone_keys.spent_today(p)
            if value is not None:
                phones.spend[p["serial"]] = value
        phones.publish()

    async def spend_loop() -> None:
        while True:
            with contextlib.suppress(Exception):
                await refresh_spend()
            await asyncio.sleep(600)
```
  In `after_run(run)` add:
```python
        phone = db.phone(run["phone_id"])
        if phone and run["status"] == "failed" and "daily AI budget" in (run.get("result") or ""):
            phones.set_note(phone["serial"], run["result"])  # the spec: the card shows a spent budget
        elif phone and run["status"] == "succeeded":
            phones.set_note(phone["serial"], "")
        with contextlib.suppress(Exception):
            await refresh_spend(phone)
```
  In `lifespan`, add `spender = asyncio.create_task(spend_loop())` after `poller`, and `spender.cancel()` after `poller.cancel()`.
- Change the `register` call to `appconnect.register(app, env=env, db=db, phones=phones, devices=devices, on_phone_ready=on_phone_ready)`.

`appconnect.py`: delete the app task-screen section and `TASK_STATUS`, `_app_task`, `_trajectory_event`. Change the signature to `def register(app: FastAPI, *, env, db, phones, devices: DeviceHub, on_phone_ready) -> None:` and remove the `/v1/models`, `/v1/tasks...` bullet from the docstring. Remove imports that become unused (check with `uvx ruff check mobile_rpa --select F401`).

`index.html`: in the viewer's action row, next to `v-rename`, add `<button type="button" class="ghost sm" id="v-pause">Pause AI</button>`.

`app.js`:
- In `renderPhones`, after the Android chip lines:
```js
    let spend = card.querySelector('.c-spend');
    if (!spend) { spend = el('span', 'chip c-spend'); card.querySelector('.chips2').appendChild(spend); }
    spend.hidden = p.spend == null; spend.textContent = p.spend == null ? '' : `$${p.spend.toFixed(2)} today`;
    if (p.paused) { tag.textContent = 'AI paused'; tag.className = 'tag offline'; }
```
- In `renderViewer`: `$('v-pause').textContent = p.paused ? 'Resume AI' : 'Pause AI';`
- The handler:
```js
$('v-pause').onclick = async () => {
  const p = viewer && phoneById(viewer.id); if (!p) return;
  try { await api(`api/phones/${p.id}/${p.paused ? 'resume' : 'pause'}`, {method: 'POST'}); toast(p.name + (p.paused ? ': AI resumed' : ': AI paused')); } catch (e) { fail(e); }
};
```

- [ ] **Step 4: Run all tests**

Run: `pytest -q`
Expected: all pass.

- [ ] **Step 5: Deploy and commit**

```bash
./deploy/deploy.sh
git add -A && git commit -q -m "per-phone pause/resume and today's spend; the old app task REST is gone (the app runs its own tasks)"
```

### Task 22: POCO end to end, v1 regression check, memory

**Files:** none in the repos, plus a new memory file `/home/arrow/.claude/projects/-home-arrow/memory/project_mobile_rpa_v2.md` and one line in `MEMORY.md`.

- [ ] **Step 1: Final APK in the dashboard**

```bash
cd ~/claude_projects/fa-portal-v2 && ./build-fa.sh && cp dist/fa-portal-v2.apk ~/claude_projects/mobile-rpa-v2/mobile_rpa/fa-portal-v2.apk
cd ~/claude_projects/mobile-rpa-v2 && ./deploy/deploy.sh
```

- [ ] **Step 2: The POCO joins v2 (needs the user once: installing an APK needs a tap on the phone)**

Create an invite (`POST /mobile2/api/app/invite`) and give the user the link. On the POCO:
1. Open the link.
2. Download and install FastAutomate v2.
3. Tap Connect.
4. Turn on the FastAutomate v2 service, and switch the first FastAutomate app's service off.

Watch `GET /mobile2/api/state` until the POCO is `online` with `key_hash` set. Expected name: "POCO F3", Android 16.

- [ ] **Step 3: One task from the dashboard, one from the app**

Run "open the settings app and tell me the Android version" from the dashboard, and "open the clock app" from the app's own Run button. Expected with credit: both `succeeded`. History shows both (the second is marked as started on the phone). The POCO card shows today's spend.

- [ ] **Step 4: v1 is unchanged**

```bash
cd ~/claude_projects/mobile-rpa && git status --short && .venv/bin/python -m pytest -q 2>&1 | tail -1
cd ~/claude_projects/fa-portal && git status --short
rtk proxy curl -s -o /dev/null -w "v1 %{http_code}\n" https://digimate.fastautomate.com/mobile/
ssh -i ~/.ssh/id_root root@192.168.100.20 "pct exec 124 -- systemctl is-active mobile-rpa mobile-rpa-v2"
```
Expected: both v1 repos clean, v1 tests pass (58), `v1 200`, both services `active`.

- [ ] **Step 5: Memory**

Write `project_mobile_rpa_v2.md` (type project) covering:
- where v2 lives: repos, CT 124 port 8091, `/mobile2`, unit `mobile-rpa-v2`
- the protocol: `agent/*` messages and `X-Agent-Key-Hash`
- per-phone OpenRouter keys: management key in v2 Settings only; keys named `fastautomate-v2-<phone id>-<name>`
- how to deploy both parts
- what the user still has to do (credit top-up)

Link `[[project_mobile_rpa_dashboard]]`, and add one pointer line to `MEMORY.md`.
