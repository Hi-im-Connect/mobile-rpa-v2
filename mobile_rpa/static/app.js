'use strict';
/* Mobile RPA dashboard. Plain JS, no build step. All URLs are relative so the app works behind a path prefix. */

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const el = (tag, cls, html) => { const n = document.createElement(tag); if (cls) n.className = cls; if (html != null) n.innerHTML = html; return n; };

const S = {
  phones: [],
  tasks: new Map(),        // task id -> task (runs inside), shown on the board
  events: new Map(),       // run id -> [events]
  settings: {},
  stats: {succeeded: 0, failed: 0},
  selected: new Set(),     // phone ids picked in the composer
  split: null,             // [{phone_id, name, instruction}] after "Preview split"
  history: [], histDone: false,
  credit: {credit: null, low: false},
  phase: new Map(),        // run id -> {text, since} : what the agent is doing right now
};

/* ================= api ================= */
async function api(path, opts = {}) {
  const init = {method: opts.method || 'GET', headers: {}};
  if (opts.body !== undefined) { init.body = JSON.stringify(opts.body); init.headers['Content-Type'] = 'application/json'; }
  const res = await fetch(path, init);
  if (res.status === 401) { showLogin(); throw new Error('Please sign in again.'); }
  const ct = res.headers.get('content-type') || '';
  const data = ct.includes('json') ? await res.json() : null;
  if (!res.ok) throw new Error(errorText(data, res.status));
  return data;
}
/* FastAPI validation errors arrive as a list of objects; turn them into one readable sentence */
function errorText(data, status) {
  const d = data && (data.detail || data.error);
  if (Array.isArray(d)) return d.map((e) => `${(e.loc || []).slice(-1)[0] || 'field'}: ${e.msg}`).join('; ');
  if (typeof d === 'string') return d;
  return `Request failed (${status})`;
}
function toast(msg, bad) {
  const t = $('toast'); t.textContent = msg; t.className = 'show' + (bad ? ' bad' : '');
  clearTimeout(toast.timer); toast.timer = setTimeout(() => { t.className = ''; }, bad ? 5200 : 2600);
}
const fail = (e) => toast(e.message || String(e), true);

/* ================= login ================= */
function showLogin() { document.body.classList.add('locked'); setTimeout(() => $('login-pass').focus(), 50); }
$('login-form').addEventListener('submit', async (e) => {
  e.preventDefault(); $('login-err').textContent = '';
  try {
    await api('api/login', {method: 'POST', body: {password: $('login-pass').value}});
    document.body.classList.remove('locked'); $('login-pass').value = ''; boot();
  } catch (err) { $('login-err').textContent = err.message; }
});

/* ================= time helpers ================= */
const parseTs = (s) => s ? new Date(s) : null;
function ago(s) {
  const d = parseTs(s); if (!d) return '';
  const sec = Math.round((Date.now() - d) / 1000);
  if (sec < 60) return 'just now'; if (sec < 3600) return Math.floor(sec / 60) + 'm ago';
  if (sec < 86400) return Math.floor(sec / 3600) + 'h ago';
  return d.toLocaleDateString(undefined, {month: 'short', day: 'numeric'}) + ' ' + d.toLocaleTimeString(undefined, {hour: '2-digit', minute: '2-digit'});
}
function took(a, b) {
  const s = parseTs(a), e = parseTs(b) || (s ? new Date() : null); if (!s) return '';
  const sec = Math.max(0, Math.round((e - s) / 1000));
  return sec < 60 ? sec + 's' : Math.floor(sec / 60) + 'm ' + String(sec % 60).padStart(2, '0') + 's';
}

/* ================= live screen (scrcpy H.264 over websocket, WebCodecs) ================= */
const wsUrl = (p) => new URL(p, location.href).href.replace(/^http/, 'ws');
/* H.265 is ~20% smaller on slow links, but Chrome only decodes it with a hardware decoder: ask first */
let H265 = false;
if ('VideoDecoder' in window) VideoDecoder.isConfigSupported({codec: 'hvc1.1.6.L93.B0'}).then((r) => { H265 = !!r.supported; }).catch(() => {});
class LiveView {
  constructor(host, phoneId, {interactive = true} = {}) {
    this.host = host; this.id = phoneId; this.interactive = interactive;
    this.closed = false; this.retry = 1000; this.ts = 0;
    host.innerHTML = '';
    this.canvas = el('canvas'); this.ctx = this.canvas.getContext('2d');
    this.ov = el('div', 'ov', 'Connecting...');
    this.badge = el('div', 'badge', '<i></i>LIVE'); this.badge.hidden = true;
    host.append(this.canvas, this.badge, this.ov);
    if (interactive) this.bindInput();
    this.start();
  }
  start() {
    if (this.closed) return;
    if (!('VideoDecoder' in window) || !window.isSecureContext && location.hostname !== 'localhost') { this.snapshots(); return; }
    const ws = this.ws = new WebSocket(wsUrl(`ws/phones/${this.id}?h265=${H265 ? 1 : 0}`));
    ws.binaryType = 'arraybuffer';
    ws.onmessage = (m) => typeof m.data === 'string' ? this.onText(JSON.parse(m.data)) : this.onFrame(new Uint8Array(m.data));
    ws.onclose = () => { if (this.closed || this.snap) return; this.status('Reconnecting...'); setTimeout(() => this.start(), this.retry); this.retry = Math.min(this.retry * 2, 8000); };
  }
  status(text) { this.ov.textContent = text; this.ov.hidden = !text; this.badge.hidden = !!text; }
  onText(msg) {
    if (msg.type === 'meta') this.configure(msg);
    else if (msg.type === 'error') { this.snapshots(msg.error); }
    else if (msg.type === 'end') { this.status('Reconnecting...'); this.retry = 500; }
  }
  configure(meta) {
    this.w = meta.width; this.h = meta.height;
    this.jpeg = meta.codec === 'jpeg'; // app-connected phone: still frames, no video decoder needed
    if (this.w && this.h) this.host.style.setProperty('--ar', `${this.w} / ${this.h}`);
    if (!meta.codec || this.jpeg) { if (this.w && this.h) this.host.style.setProperty('--ar', `${this.w} / ${this.h}`); return; }
    if (this.decoder && this.codec === meta.codec && this.decoder.state === 'configured') { this.needKey = true; return; }
    try { this.decoder && this.decoder.state !== 'closed' && this.decoder.close(); } catch (e) {}
    this.codec = meta.codec; this.needKey = true;
    this.decoder = new VideoDecoder({
      output: (frame) => this.draw(frame),
      error: () => { this.needKey = true; this.reset(); },
    });
    this.decoder.configure({codec: meta.codec, optimizeForLatency: true});
  }
  reset() {
    if (this.closed || !this.codec) return;
    try { this.decoder.reset(); this.decoder.configure({codec: this.codec, optimizeForLatency: true}); } catch (e) { this.snapshots(); }
  }
  onFrame(buf) {
    const flags = buf[0], data = buf.subarray(1);
    if (flags & 4) { // JPEG frame
      createImageBitmap(new Blob([data], {type: 'image/jpeg'})).then((bmp) => {
        if (this.canvas.width !== bmp.width || this.canvas.height !== bmp.height) { this.canvas.width = bmp.width; this.canvas.height = bmp.height; }
        this.ctx.drawImage(bmp, 0, 0); bmp.close();
        if (!this.ov.hidden) { this.status(''); this.retry = 1000; }
      }).catch(() => {});
      return;
    }
    if (flags & 1) { this.config = data.slice(); return; }
    const key = (flags & 2) !== 0;
    if (!this.decoder || this.decoder.state !== 'configured') return;
    if (this.needKey && !key) return;
    if (this.decoder.decodeQueueSize > 8) { this.needKey = true; if (!key) return; }
    let payload = data;
    if (key && this.config) { payload = new Uint8Array(this.config.length + data.length); payload.set(this.config); payload.set(data, this.config.length); }
    try {
      this.decoder.decode(new EncodedVideoChunk({type: key ? 'key' : 'delta', timestamp: (this.ts += 33333), data: payload}));
      this.needKey = false;
    } catch (e) { this.needKey = true; }
  }
  draw(frame) {
    if (this.canvas.width !== frame.displayWidth || this.canvas.height !== frame.displayHeight) {
      this.canvas.width = frame.displayWidth; this.canvas.height = frame.displayHeight;
    }
    this.ctx.drawImage(frame, 0, 0); frame.close();
    if (!this.ov.hidden) { this.status(''); this.retry = 1000; }
  }
  point(e) {
    const r = this.canvas.getBoundingClientRect();
    const w = this.w || this.canvas.width, h = this.h || this.canvas.height;
    const scale = Math.min(r.width / w, r.height / h);
    const ox = (r.width - w * scale) / 2, oy = (r.height - h * scale) / 2;
    return {x: Math.round((e.clientX - r.left - ox) / scale), y: Math.round((e.clientY - r.top - oy) / scale)};
  }
  send(msg) { if (this.ws && this.ws.readyState === 1) { this.ws.send(JSON.stringify(msg)); return true; } return false; }
  bindInput() {
    let down = false, pending = null;
    const c = this.canvas;
    c.addEventListener('pointerdown', (e) => { if (e.button !== 0) return; down = true; c.setPointerCapture(e.pointerId); this.send({t: 'touch', a: 0, ...this.point(e)}); e.preventDefault(); });
    c.addEventListener('pointermove', (e) => {
      if (!down) return; pending = this.point(e);
      if (!this.raf) this.raf = requestAnimationFrame(() => { this.raf = 0; if (pending) this.send({t: 'touch', a: 2, ...pending}); pending = null; });
    });
    const up = (e) => { if (!down) return; down = false; this.send({t: 'touch', a: 1, ...this.point(e)}); };
    c.addEventListener('pointerup', up); c.addEventListener('pointercancel', up);
    c.addEventListener('wheel', (e) => { e.preventDefault(); const p = this.point(e);
      this.send({t: 'scroll', x: p.x, y: p.y, h: Math.max(-4, Math.min(4, -e.deltaX / 60)), v: Math.max(-4, Math.min(4, -e.deltaY / 60))}); }, {passive: false});
    c.addEventListener('contextmenu', (e) => { e.preventDefault(); this.key('back'); });
  }
  key(name) { if (!this.send({t: 'key', key: name})) api(`api/phones/${this.id}/key`, {method: 'POST', body: {key: name}}).catch(fail); }
  text(s) { return this.send({t: 'text', s}); }
  snapshots(reason) {
    if (this.snap || this.closed) return;
    this.snap = true; try { this.ws && this.ws.close(); } catch (e) {}
    const img = el('img'); img.alt = 'Phone screen';
    this.canvas.replaceWith(img); this.badge.innerHTML = 'SNAPSHOT'; this.badge.classList.add('snap');
    this.status(reason ? 'Live video unavailable, showing snapshots' : 'Loading...');
    const tick = () => {
      if (this.closed) return;
      const next = new Image();
      next.onload = () => { img.src = next.src; this.status(''); setTimeout(tick, 900); };
      next.onerror = () => { this.status('Phone not reachable'); setTimeout(tick, 3000); };
      next.src = `api/phones/${this.id}/screen.jpg?t=${Date.now()}`;
    };
    tick();
  }
  destroy() {
    this.closed = true; this.badge.hidden = true;
    try { this.ws && this.ws.close(); } catch (e) {}
    try { this.decoder && this.decoder.state !== 'closed' && this.decoder.close(); } catch (e) {}
  }
}

/* ================= router ================= */
const PAGES = ['phones', 'tasks', 'history'];
function route() {
  const page = PAGES.includes(location.hash.slice(1)) ? location.hash.slice(1) : 'phones';
  document.querySelectorAll('.page').forEach((p) => p.classList.toggle('on', p.id === 'page-' + page));
  document.querySelectorAll('[data-page]').forEach((a) => a.classList.toggle('on', a.dataset.page === page));
  if (page === 'history') loadHistory(true);
  if (page === 'phones' && booted) thumbTick(true);
  if (page === 'tasks') renderChips();
}
window.addEventListener('hashchange', route);
$('new-task-btn').onclick = () => { location.hash = '#tasks'; setTimeout(() => $('prompt').focus(), 60); };

/* ================= header ================= */
function bump(id, value) {
  const n = $(id); if (n.textContent === String(value)) return;
  n.textContent = value; n.classList.remove('bump'); void n.offsetWidth; n.classList.add('bump');
}
function renderKpis() {
  const online = S.phones.filter((p) => p.status === 'online' || p.status === 'busy').length;
  bump('k-online', online); $('k-total').textContent = '/ ' + S.phones.length;
  bump('k-running', S.phones.filter((p) => p.status === 'busy').length);
  bump('k-done', S.stats.succeeded); bump('k-failed', S.stats.failed);
}
function renderCredit() {
  const c = S.credit;
  $('bk-credit').hidden = c.credit == null;
  if (c.credit != null) { $('k-credit').textContent = '$' + c.credit.toFixed(2); $('bk-credit').classList.toggle('low', !!c.low); }
}
$('bk-credit').onclick = async () => {
  try { S.credit = await api('api/credit/refresh', {method: 'POST'}); renderCredit(); syncSend(); toast('AI credit: $' + (S.credit.credit ?? 0).toFixed(2)); }
  catch (e) { fail(e); }
};
$('theme-btn').onclick = () => {
  const root = document.documentElement;
  const dark = root.dataset.theme ? root.dataset.theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
  root.dataset.theme = dark ? 'light' : 'dark';
  try { localStorage.setItem('mrpa-theme', root.dataset.theme); } catch (e) {}
};

/* ================= phones page ================= */
const ICON = {
  android: '<svg viewBox="0 0 24 24"><path d="M5 18v-5a7 7 0 0 1 14 0v5z"/><path d="M8 6.5 6.5 4M16 6.5 17.5 4"/><circle cx="9.5" cy="12" r=".6"/><circle cx="14.5" cy="12" r=".6"/></svg>',
  netbird: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/></svg>',
  wifi: '<svg viewBox="0 0 24 24"><path d="M2.5 9a14 14 0 0 1 19 0M5.5 12.5a9.5 9.5 0 0 1 13 0M8.8 16a4.8 4.8 0 0 1 6.4 0"/><circle cx="12" cy="19.2" r=".8"/></svg>',
  usb: '<svg viewBox="0 0 24 24"><path d="M12 3v15M9 6l3-3 3 3M8 11v2l4 3M16 9v3l-4 3"/><circle cx="12" cy="19.5" r="1.5"/></svg>',
  virtual: '<svg viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6" rx="1"/><path d="M9 1.5V4M15 1.5V4M9 20v2.5M15 20v2.5M1.5 9H4M1.5 15H4M20 9h2.5M20 15h2.5"/></svg>',
};
const LINKS = {
  app: {icon: '<svg viewBox="0 0 24 24"><rect x="7" y="2.5" width="10" height="19" rx="2.2"/><path d="M11 18.5h2"/></svg>', label: 'App'},
  netbird: {icon: ICON.netbird, label: 'NetBird'},
  wifi: {icon: ICON.wifi, label: 'Wi-Fi'},
  usb: {icon: ICON.usb, label: 'USB'},
  virtual: {icon: ICON.virtual, label: 'Virtual'},
};
const STATUS_LABEL = {online: 'Online', busy: 'Working', preparing: 'Setting up', offline: 'Offline', unauthorized: 'Allow USB debugging'};
const phoneById = (id) => S.phones.find((p) => p.id === id);
const runById = (id) => { for (const t of S.tasks.values()) { const r = t.runs.find((x) => x.id === id); if (r) return r; } return null; };

function renderPhones() {
  const grid = $('phone-grid');
  const keep = new Set();
  for (const p of S.phones) {
    keep.add('ph-' + p.id);
    let card = $('ph-' + p.id);
    if (!card) {
      card = el('button', 'ph'); card.id = 'ph-' + p.id; card.type = 'button';
      card.innerHTML = `<div class="top"><span class="led"></span><b></b><span class="tag"></span></div>
        <div class="frame"><div class="screen"><img alt="" loading="lazy"></div></div>
        <div class="meta"><span class="m1"></span><div class="chips2"><span class="chip c-android"></span><span class="chip c-link"></span></div></div>
        <div class="doing" hidden></div><div class="note" hidden></div>`;
      card.onclick = () => openViewer(p.id);
      grid.appendChild(card);
    }
    card.className = 'ph ' + p.status;
    card.querySelector('.led').className = 'led ' + p.status + (p.status === 'busy' ? ' pulse' : '');
    card.querySelector('.top b').textContent = p.name;
    const tag = card.querySelector('.tag'); tag.className = 'tag ' + p.status; tag.textContent = STATUS_LABEL[p.status] || p.status;
    card.title = where(p);
    card.querySelector('.m1').textContent = p.link === 'virtual' ? 'Virtual phone' : (p.model || 'Android phone');
    const andr = card.querySelector('.c-android');
    andr.hidden = !p.android; andr.innerHTML = ICON.android + 'Android ' + esc(p.android);
    const link = LINKS[p.link] || LINKS.wifi;
    const lc = card.querySelector('.c-link'); lc.className = 'chip c-link ' + (p.link || 'wifi'); lc.innerHTML = link.icon + link.label;
    const run = p.run_id ? runById(p.run_id) : null;
    const doing = card.querySelector('.doing'); doing.hidden = !run;
    if (run) { const ph = S.phase.get(run.id); doing.textContent = (ph ? ph.text + ': ' : 'Working on: ') + run.instruction; }
    const note = card.querySelector('.note'); note.hidden = !p.note; note.textContent = p.note || '';
    const screen = card.querySelector('.screen');
    if (p.status === 'offline' || p.status === 'unauthorized') screen.dataset.off = '1'; else delete screen.dataset.off;
  }
  grid.querySelectorAll('.ph').forEach((c) => { if (!keep.has(c.id)) c.remove(); });
  $('phones-empty').hidden = S.phones.length > 0;
  renderKpis();
}
/* thumbnails: refreshed only while the Phones page is on screen, one request in flight per phone */
function thumbTick(once) {
  if (!document.hidden && $('page-phones').classList.contains('on') && !document.body.classList.contains('locked')) {
    for (const p of S.phones) {
      const img = document.querySelector(`#ph-${p.id} img`);
      if (!img || img.dataset.busy || p.status !== 'online') continue;
      img.dataset.busy = '1';
      const next = new Image();
      next.onload = () => { img.src = next.src; delete img.dataset.busy; img.parentNode.style.setProperty('--ar', `${next.naturalWidth} / ${next.naturalHeight}`); };
      next.onerror = () => { delete img.dataset.busy; };
      next.src = `api/phones/${p.id}/screen.jpg?t=${Date.now()}`;
    }
  }
  if (!once) setTimeout(thumbTick, 10000);
}

$('add-phone-btn').onclick = addPhone;
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

/* ================= live viewer ================= */
let viewer = null;
function openViewer(phoneId) {
  const p = phoneById(phoneId); if (!p) return;
  closeViewer();
  $('viewer').hidden = false;
  viewer = {id: phoneId, live: new LiveView($('v-screen'), phoneId, {interactive: true})};
  renderViewer();
}
function closeViewer() {
  if (!viewer) return;
  viewer.live.destroy(); viewer = null; $('viewer').hidden = true;
}
// how a phone is reached, for tooltips and the viewer (app phones have an internal serial)
const where = (p) => (p.link === 'app' ? 'FastAutomate app' : p.serial);

function renderViewer() {
  if (!viewer) return;
  const p = phoneById(viewer.id); if (!p) { closeViewer(); return; }
  $('v-name').textContent = p.name;
  $('v-sub').textContent = [where(p), p.model, p.android && 'Android ' + p.android].filter(Boolean).join('  /  ');
  $('v-led').className = 'led ' + p.status;
  $('v-tag').className = 'tag ' + p.status; $('v-tag').textContent = STATUS_LABEL[p.status] || p.status;
  const run = p.run_id ? runById(p.run_id) : null;
  $('v-now').innerHTML = run ? `<div class="instr">${esc(run.instruction)}</div><div class="now" style="margin-top:6px"><span class="spin"></span><span class="txt"></span><span class="el"></span></div>`
    : `<ul class="tips"><li>No task running: <b>you are in control</b>.</li><li><b>Click</b> to tap, <b>drag</b> to swipe, <b>scroll</b> to scroll.</li><li><b>Right-click</b> is Back.</li></ul>`;
  if (run) renderNow($('v-now').querySelector('.now'), run);
  $('v-steps').textContent = run ? `${run.steps || 0} steps  ${took(run.started_at, run.ended_at)}` : '';
  $('v-stop').hidden = !run; $('v-stop').onclick = () => run && stopRun(run.id);
  renderLog($('v-log'), run ? (S.events.get(run.id) || []) : [], true);
}
$('v-close').onclick = closeViewer;
$('viewer').addEventListener('click', (e) => { if (e.target.id === 'viewer') closeViewer(); });
document.querySelectorAll('.v-keys button').forEach((b) => b.onclick = () => viewer && viewer.live.key(b.dataset.key));
$('v-type').addEventListener('submit', (e) => {
  e.preventDefault(); const v = $('v-text').value; if (!v || !viewer) return;
  if (viewer.live.text(v)) { $('v-text').value = ''; } else toast('Typing needs the live view.', true);
});
$('v-rename').onclick = () => {
  const p = viewer && phoneById(viewer.id); if (!p) return;
  openModal({title: 'Rename phone', body: `<label for="m-rn">Name</label><input type="text" id="m-rn" maxlength="60" value="${esc(p.name)}">`, go: 'Save', focus: 'm-rn',
    submit: async () => { await api(`api/phones/${p.id}`, {method: 'PATCH', body: {name: $('m-rn').value}}); }});
};
$('v-remove').onclick = () => {
  const p = viewer && phoneById(viewer.id); if (!p) return;
  openModal({title: 'Remove ' + p.name + '?', sub: 'The dashboard forgets this phone and disconnects adb. Its task history stays.', go: 'Remove', danger: true,
    submit: async () => { await api(`api/phones/${p.id}`, {method: 'DELETE'}); closeViewer(); toast(p.name + ' removed'); }});
};
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  if ($('modal-wrap').classList.contains('on')) closeModal();
  else if (document.body.classList.contains('so-open')) closeSo();
  else closeViewer();
});

/* ================= "now" line ================= */
function renderNow(box, r) {
  if (!box) return;
  const ph = S.phase.get(r.id);
  box.hidden = !isActive(r);
  if (!isActive(r)) return;
  box.querySelector('.txt').textContent = ph ? ph.text : (r.status === 'queued' ? 'Waiting to start' : 'Starting');
  const sec = ph ? Math.max(0, Math.round((Date.now() - ph.since) / 1000)) : 0;
  box.querySelector('.el').textContent = sec >= 3 ? sec + 's' : '';
}
function lastPhase(events) {
  for (let i = events.length - 1; i >= 0; i--) if (events[i].kind === 'phase') return {text: events[i].text, since: Date.parse(events[i].ts)};
  return null;
}
const HIDDEN_KINDS = new Set(['phase', 'status']);

/* ================= step log ================= */
const KIND_LABEL = {plan: 'Plan', step: 'Step', action: 'Do', ok: 'OK', error: 'Error', think: 'Think', answer: 'Answer', status: 'Info', done: 'Done', fail: 'Failed'};
function evNode(ev) {
  const n = el('div', 'ev ' + ev.kind);
  n.innerHTML = `<span class="k">${KIND_LABEL[ev.kind] || esc(ev.kind)}</span><span class="x2"></span>`;
  n.lastChild.textContent = ev.text;
  return n;
}
function renderLog(box, events, stick) {
  const atEnd = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
  box.replaceChildren(...events.filter((e) => !HIDDEN_KINDS.has(e.kind)).map(evNode));
  if (stick || atEnd) box.scrollTop = box.scrollHeight;
}
function appendLog(box, ev) {
  if (HIDDEN_KINDS.has(ev.kind)) return;
  const atEnd = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
  box.appendChild(evNode(ev));
  if (atEnd) box.scrollTop = box.scrollHeight;
}

/* ================= composer ================= */
const EXAMPLES = [
  'Open Chrome, search for the weather in Cairo and tell me the temperature',
  'Each phone installs a different free note-taking app from the Play Store and opens it',
  'Open Settings and tell me the Android version and free storage',
];
$('examples').append(...EXAMPLES.map((x) => { const b = el('button'); b.type = 'button'; b.textContent = x.length > 52 ? x.slice(0, 50) + '...' : x; b.title = x; b.onclick = () => { $('prompt').value = x; clearSplit(); syncSend(); }; return b; }));
function renderChips() {
  const box = $('phone-chips'); box.innerHTML = '';
  if (!S.phones.length) { box.innerHTML = '<span class="none">No phones yet. Add one on the Phones page.</span>'; }
  for (const p of S.phones) {
    const usable = p.status === 'online';
    if (!usable) S.selected.delete(p.id);
    const c = el('button', 'pick' + (S.selected.has(p.id) ? ' sel' : '') + (usable ? '' : ' dis'));
    c.type = 'button'; c.disabled = !usable; c.title = usable ? where(p) : (STATUS_LABEL[p.status] || p.status);
    c.innerHTML = `<span class="led ${p.status}"></span>${esc(p.name)}`;
    c.onclick = () => { S.selected.has(p.id) ? S.selected.delete(p.id) : S.selected.add(p.id); clearSplit(); renderChips(); };
    box.appendChild(c);
  }
  syncSend();
}
$('pick-all').onclick = () => { S.phones.forEach((p) => p.status === 'online' && S.selected.add(p.id)); clearSplit(); renderChips(); };
$('prompt').addEventListener('input', () => { clearSplit(); syncSend(); });
function clearSplit() { S.split = null; $('split-box').hidden = true; }
function syncSend() {
  const n = S.selected.size, text = $('prompt').value.trim();
  $('split-btn').hidden = n < 2;
  $('split-btn').disabled = !text;
  $('run-btn').disabled = !text || !n || S.credit.low;
  $('run-btn').textContent = n > 1 ? `Run on ${n} phones` : 'Run';
  $('send-hint').textContent = S.credit.low ? 'Add AI credit to run tasks.' : !text ? 'Describe the task first.' : !n ? 'Pick at least one phone.'
    : n === 1 ? 'Runs exactly as written.' : S.split ? 'Edit any line, then Run.' : 'Run splits it automatically. Preview to check first.';
}
async function doSplit() {
  const ids = [...S.selected];
  $('split-btn').disabled = true; $('split-btn').textContent = 'Splitting...';
  try {
    const res = await api('api/split', {method: 'POST', body: {prompt: $('prompt').value, phone_ids: ids}});
    S.split = res.instructions; renderSplit(); return true;
  } catch (e) { fail(e); return false; }
  finally { $('split-btn').textContent = 'Preview split'; syncSend(); }
}
function renderSplit() {
  $('split-box').hidden = false; $('split-cnt').textContent = S.split.length + ' phones';
  $('split-list').replaceChildren(...S.split.map((row, i) => {
    const r = el('div', 'split-row'); r.innerHTML = `<span class="who" title="${esc(row.name)}">${esc(row.name)}</span><textarea rows="2"></textarea>`;
    const ta = r.querySelector('textarea'); ta.value = row.instruction; ta.oninput = () => { S.split[i].instruction = ta.value; };
    return r;
  }));
  syncSend();
}
$('split-btn').onclick = doSplit;
$('split-redo').onclick = doSplit;
$('run-btn').onclick = async () => {
  const prompt = $('prompt').value.trim(); const ids = [...S.selected];
  if (!prompt || !ids.length) return;
  if (!S.settings.ready) { toast('Add an API key in Settings first.', true); openSettings(); return; }
  if (ids.length > 1 && !S.split && !(await doSplit())) return;
  const runs = ids.length === 1 ? [{phone_id: ids[0], instruction: prompt}] : S.split.map((r) => ({phone_id: r.phone_id, instruction: r.instruction.trim() || prompt}));
  $('run-btn').disabled = true;
  try {
    const task = await api('api/tasks', {method: 'POST', body: {prompt, reasoning: $('opt-reasoning').checked, max_steps: +$('opt-steps').value || 30, runs}});
    clearFinished();
    if (!S.tasks.has(task.id)) S.tasks.set(task.id, task); // SSE may already hold a newer copy
    renderBoard();
    $('prompt').value = ''; S.selected.clear(); clearSplit(); renderChips();
    toast(runs.length > 1 ? `Running on ${runs.length} phones` : 'Running');
  } catch (e) { fail(e); } finally { syncSend(); }
};

/* ================= running board ================= */
const isActive = (r) => r.status === 'queued' || r.status === 'running';
function renderBoard() {
  const board = $('board');
  const tasks = [...S.tasks.values()].sort((a, b) => b.id - a.id);
  const keep = new Set();
  for (const t of tasks) {
    const gid = 'rg-' + t.id; keep.add(gid);
    let g = $(gid);
    if (!g) {
      g = el('div', 'run-group'); g.id = gid;
      g.innerHTML = `<div class="rg-head"><div class="p"></div><button class="ghost sm stop-all">Stop all</button><button class="ghost sm dismiss">Clear</button></div><div class="tiles"></div>`;
      g.querySelector('.stop-all').onclick = () => api(`api/tasks/${t.id}/stop`, {method: 'POST'}).catch(fail);
      g.querySelector('.dismiss').onclick = () => { S.tasks.delete(t.id); renderBoard(); };
      g.querySelector('.dismiss').textContent = 'Move to History';
      board.appendChild(g);
    }
    const active = t.runs.filter(isActive).length;
    g.querySelector('.p').innerHTML = `${esc(t.prompt)}<small>${t.runs.length} phone${t.runs.length > 1 ? 's' : ''}  /  ${active ? active + ' working' : 'finished'}  /  started ${ago(t.created_at)}</small>`;
    g.querySelector('.stop-all').hidden = !active;
    g.querySelector('.dismiss').hidden = !!active;
    const tiles = g.querySelector('.tiles');
    for (const r of t.runs) updateTile(tiles, r);
  }
  board.querySelectorAll('.run-group').forEach((g) => { if (!keep.has(g.id)) g.remove(); });
  // newest first
  tasks.forEach((t) => board.appendChild($('rg-' + t.id)));
  const running = tasks.reduce((n, t) => n + t.runs.filter(isActive).length, 0);
  $('board-cnt').textContent = running ? running + ' running' : '';
  $('board-empty').hidden = tasks.length > 0;
}
function updateTile(tiles, r) {
  let tile = $('tile-' + r.id);
  if (!tile) {
    tile = el('div', 'tile'); tile.id = 'tile-' + r.id;
    tile.innerHTML = `<div class="screen"></div><div class="info"><div class="t1"><b></b><span class="tag"></span></div>
      <div class="instr"></div><div class="now" hidden><span class="spin"></span><span class="txt"></span><span class="el"></span></div><div class="res" hidden></div><div class="log"></div><div class="foot"><span class="meta"></span><button class="ghost sm stop">Stop</button></div></div>`;
    tile.querySelector('.screen').onclick = () => openViewer(r.phone_id);
    tile.querySelector('.stop').onclick = () => stopRun(r.id);
    tiles.appendChild(tile);
    renderLog(tile.querySelector('.log'), S.events.get(r.id) || [], true);
  }
  tile.className = 'tile ' + r.status;
  tile.querySelector('.t1 b').textContent = r.phone_name;
  const tag = tile.querySelector('.tag'); tag.className = 'tag ' + r.status; tag.textContent = r.status;
  tile.querySelector('.instr').textContent = r.instruction; tile.querySelector('.instr').title = r.instruction;
  tile.querySelector('.meta').textContent = `${r.steps || 0} steps  ${took(r.started_at, r.ended_at)}`;
  tile.querySelector('.stop').hidden = !isActive(r);
  renderNow(tile.querySelector('.now'), r);
  const res = tile.querySelector('.res');
  res.hidden = isActive(r) || !r.result; res.textContent = r.result || '';
  res.className = 'res' + (r.status === 'succeeded' ? ' good' : ' bad');
  const scr = tile.querySelector('.screen');
  const want = isActive(r) ? 'working' : 'shot';
  if (scr.dataset.mode !== want) {
    scr.dataset.mode = want;
    scr.innerHTML = want === 'working'
      ? '<div class="ov"><span class="spin"></span> Working on the phone</div>'
      : `<img alt="Final screen" src="api/runs/${r.id}/shot.jpg" onerror="this.replaceWith(Object.assign(document.createElement('div'), {className: 'ov', textContent: 'No final screenshot'}))">`;
  }
}
/* finished tasks leave the board when a new one starts (they stay in History) */
function clearFinished() {
  for (const [id, t] of S.tasks) if (!t.runs.some(isActive)) S.tasks.delete(id);
}
async function stopRun(runId) {
  try { await api(`api/runs/${runId}/stop`, {method: 'POST'}); toast('Stopping...'); } catch (e) { fail(e); }
}
setInterval(() => { // keep elapsed times ticking
  for (const t of S.tasks.values()) for (const r of t.runs) if (isActive(r)) { const tile = $('tile-' + r.id); if (tile) { tile.querySelector('.meta').textContent = `${r.steps || 0} steps  ${took(r.started_at, r.ended_at)}`; renderNow(tile.querySelector('.now'), r); } }
  if (viewer) { const p = phoneById(viewer.id); const run = p && p.run_id ? runById(p.run_id) : null; if (run) $('v-steps').textContent = `${run.steps || 0} steps  ${took(run.started_at, run.ended_at)}`; }
}, 1000);

/* ================= history ================= */
const STATUS_TXT = {succeeded: 'Done', failed: 'Failed', stopped: 'Stopped', interrupted: 'Interrupted', running: 'Running', queued: 'Queued'};
async function loadHistory(reset) {
  if (reset) { S.history = []; S.histDone = false; }
  try {
    const rows = await api(`api/tasks?limit=30&offset=${S.history.length}`);
    S.history.push(...rows); S.histDone = rows.length < 30; renderHistory();
  } catch (e) { fail(e); }
}
function renderHistory() {
  $('hist-body').replaceChildren(...S.history.map((t) => {
    const tr = el('tr');
    const ends = t.runs.map((r) => r.ended_at).filter(Boolean).sort();
    const steps = t.runs.reduce((n, r) => n + (r.steps || 0), 0);
    tr.innerHTML = `<td class="when">${esc(ago(t.created_at))}</td><td class="prompt"><div></div></td><td class="num">${t.runs.length}</td>
      <td><div class="dots">${t.runs.map((r) => `<span class="tag ${r.status}" title="${esc(r.phone_name)}">${esc(STATUS_TXT[r.status] || r.status)}</span>`).join('')}</div></td>
      <td class="num">${steps}</td><td class="num">${esc(took(t.created_at, ends[ends.length - 1] || null))}</td>`;
    tr.querySelector('.prompt div').textContent = t.prompt;
    tr.onclick = () => openTask(t.id);
    return tr;
  }));
  $('hist-empty').hidden = S.history.length > 0;
  $('hist-more').hidden = S.histDone;
}
$('hist-more').onclick = () => loadHistory(false);

async function openTask(taskId) {
  let t; try { t = await api(`api/tasks/${taskId}`); } catch (e) { fail(e); return; }
  $('so-title').textContent = 'Task #' + t.id;
  $('so-sub').textContent = new Date(t.created_at).toLocaleString() + '  /  ' + (t.reasoning ? 'plan, then act' : 'direct') + '  /  max ' + t.max_steps + ' steps';
  const body = $('so-body'); body.innerHTML = '';
  const p = el('div', 'run-card'); p.innerHTML = '<div class="t1"><b>Prompt</b></div><div class="instr"></div>'; p.querySelector('.instr').textContent = t.prompt; body.appendChild(p);
  for (const r of t.runs) {
    const c = el('div', 'run-card');
    c.innerHTML = `<div class="t1"><b></b><span class="tag ${r.status}">${esc(STATUS_TXT[r.status] || r.status)}</span></div>
      <div class="instr"></div><div class="res ${r.status === 'succeeded' ? '' : 'bad'}"></div>
      <img class="shot" alt="" loading="lazy" src="api/runs/${r.id}/shot.jpg" onerror="this.remove()" style="max-width:180px;border-radius:12px;margin:8px 0">
      <div class="field-note">${r.steps || 0} steps  /  ${esc(took(r.started_at, r.ended_at))}</div>
      <details><summary>Step log (${r.events.length})</summary><div class="log"></div></details>`;
    c.querySelector('.t1 b').textContent = r.phone_name;
    c.querySelector('.instr').textContent = r.instruction;
    c.querySelector('.res').textContent = r.result || '';
    renderLog(c.querySelector('.log'), r.events, false);
    body.appendChild(c);
  }
  const foot = $('so-foot'); foot.innerHTML = '';
  const again = el('button', 'primary', 'Run again'); again.onclick = () => rerun(t); foot.appendChild(again);
  openSo();
}
function rerun(t) {
  closeSo(); location.hash = '#tasks';
  $('prompt').value = t.prompt; S.selected.clear();
  const rows = [];
  for (const r of t.runs) { const p = phoneById(r.phone_id); if (p && p.status === 'online') { S.selected.add(p.id); rows.push({phone_id: p.id, name: p.name, instruction: r.instruction}); } }
  renderChips();
  if (rows.length > 1) { S.split = rows; renderSplit(); } else clearSplit();
  if (rows.length < t.runs.length) toast(`${t.runs.length - rows.length} of those phones are not online right now.`, true);
}

/* ================= settings ================= */
$('settings-btn').onclick = () => openSettings();
function openSettings() {
  const s = S.settings;
  $('so-title').textContent = 'Settings'; $('so-sub').textContent = 'OpenRouter keys, models and the limits for every task.';
  $('so-body').innerHTML = `
    <label for="s-url">Provider URL <small>(OpenAI format)</small></label><input type="text" id="s-url" value="${esc(s.base_url)}">
    <p class="field-note">OpenRouter (https://openrouter.ai/api/v1): each phone gets its own capped key from the management key.
      Any other provider, for example Gemini (https://generativelanguage.googleapis.com/v1beta/openai): the phones share the API key below.</p>
    <label for="s-key">API key <small>(the dashboard's own; shared with phones on non-OpenRouter providers)</small></label>
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
    base_url: $('s-url').value, planner_model: $('s-planner').value, executor_model: $('s-exec').value, daily_cap_usd: $('s-cap').value,
    max_steps: $('s-steps').value, timeout_minutes: $('s-timeout').value, vision: $('s-vision').checked ? '1' : '0',
  };
  if ($('s-key').value.trim()) body.api_key = $('s-key').value.trim();
  if ($('s-mkey').value.trim()) body.management_key = $('s-mkey').value.trim();
  S.settings = await api('api/settings', {method: 'PUT', body});
  $('s-key').value = ''; $('s-mkey').value = ''; $('opt-steps').value = S.settings.max_steps;
  try { S.credit = await api('api/credit/refresh', {method: 'POST'}); renderCredit(); syncSend(); } catch (e) {}
}

/* ================= slide-over + modal ================= */
function openSo() { document.body.classList.add('so-open'); $('so').setAttribute('aria-hidden', 'false'); }
function closeSo() { document.body.classList.remove('so-open'); $('so').setAttribute('aria-hidden', 'true'); }
$('so-close').onclick = closeSo; $('overlay').onclick = closeSo;
let modalSubmit = null;
function openModal({title, sub = '', body = '', go = 'OK', danger = false, focus, submit}) {
  $('m-go').hidden = false;
  $('m-title').textContent = title; $('m-sub').innerHTML = sub; $('m-body').innerHTML = body; $('m-err').textContent = '';
  $('m-go').textContent = go; $('m-go').className = 'go' + (danger ? ' red' : ''); $('m-go').disabled = false;
  modalSubmit = submit; $('modal-wrap').classList.add('on');
  setTimeout(() => (focus ? $(focus) : $('m-go')).focus(), 40);
}
function closeModal() { $('modal-wrap').classList.remove('on'); modalSubmit = null; }
$('m-cancel').onclick = closeModal;
$('modal-wrap').addEventListener('click', (e) => { if (e.target.id === 'modal-wrap') closeModal(); });
$('modal').addEventListener('submit', async (e) => {
  e.preventDefault(); if (!modalSubmit) return;
  const label = $('m-go').textContent; $('m-go').disabled = true; $('m-go').textContent = 'Working...'; $('m-err').textContent = '';
  try { await modalSubmit(); closeModal(); } catch (err) { $('m-err').textContent = err.message; }
  finally { $('m-go').disabled = false; $('m-go').textContent = label; }
});

/* ================= live events (SSE) ================= */
let source = null;
function connectEvents() {
  if (source) source.close();
  source = new EventSource('api/events');
  source.onopen = () => {
    $('engine').classList.remove('off'); $('engine-label').textContent = 'ENGINE LIVE';
    if (source.lost) { source.lost = false; refresh().catch(() => {}); } // resync whatever happened while we were away
  };
  source.onerror = () => {
    source.lost = true;
    $('engine').classList.add('off'); $('engine-label').textContent = 'RECONNECTING';
    if (source.readyState === EventSource.CLOSED) { setTimeout(() => refresh().then(connectEvents).catch(() => setTimeout(connectEvents, 4000)), 2000); }
  };
  source.addEventListener('phones', (m) => { S.phones = JSON.parse(m.data); renderPhones(); renderChips(); renderViewer(); });
  source.addEventListener('settings', (m) => { S.settings = JSON.parse(m.data); });
  source.addEventListener('task', (m) => { const t = JSON.parse(m.data); if (!S.tasks.has(t.id)) { S.tasks.set(t.id, t); renderBoard(); } });
  source.addEventListener('run', (m) => {
    const r = JSON.parse(m.data); const t = S.tasks.get(r.task_id);
    if (t) { const i = t.runs.findIndex((x) => x.id === r.id); if (i >= 0) t.runs[i] = r; else t.runs.push(r); renderBoard(); }
    else api(`api/tasks/${r.task_id}`).then((full) => { if (!S.tasks.has(full.id)) { S.tasks.set(full.id, full); renderBoard(); } }).catch(() => {});
    if (!isActive(r) && r.ended_at) { if (r.status === 'succeeded') S.stats.succeeded++; else S.stats.failed++; renderKpis(); if ($('page-history').classList.contains('on')) loadHistory(true); }
    renderViewer();
  });
  source.addEventListener('credit', (m) => { S.credit = JSON.parse(m.data); renderCredit(); syncSend(); });
  source.addEventListener('run_event', (m) => {
    const ev = JSON.parse(m.data);
    if (ev.kind === 'phase') {
      S.phase.set(ev.run_id, {text: ev.text, since: Date.now()});
      const r0 = runById(ev.run_id); const tile0 = $('tile-' + ev.run_id);
      if (r0 && tile0) renderNow(tile0.querySelector('.now'), r0);
      renderViewer(); renderPhones();
    }
    const list = S.events.get(ev.run_id) || []; list.push(ev); S.events.set(ev.run_id, list);
    const r = runById(ev.run_id); if (r && ev.steps != null) r.steps = ev.steps;
    const tile = $('tile-' + ev.run_id); if (tile) appendLog(tile.querySelector('.log'), ev);
    if (viewer) { const p = phoneById(viewer.id); if (p && p.run_id === ev.run_id) appendLog($('v-log'), ev); }
  });
}

/* ================= boot ================= */
async function refresh() {
  const st = await api('api/state');
  document.body.classList.remove('locked');
  S.phones = st.phones; S.settings = st.settings; S.stats = st.stats; S.credit = st.credit || S.credit;
  renderCredit();
  $('opt-steps').value = S.settings.max_steps || 30;
  $('opt-reasoning').checked = S.settings.reasoning !== '0';
  const activeIds = new Set(st.active_tasks.map((t) => t.id));
  for (const id of S.tasks.keys()) if (!activeIds.has(id)) st.active_tasks.push({id, stale: true});
  for (const t of st.active_tasks) {
    const full = t.stale ? await api(`api/tasks/${t.id}`).catch(() => null) : t;
    if (!full) { S.tasks.delete(t.id); continue; }
    for (const r of full.runs) {
      S.events.set(r.id, r.events || await api(`api/runs/${r.id}/events`));
      const ph = lastPhase(S.events.get(r.id)); if (ph) S.phase.set(r.id, ph);
      delete r.events;
    }
    S.tasks.set(full.id, full);
    for (const r of full.runs) { const tile = $('tile-' + r.id); if (tile) renderLog(tile.querySelector('.log'), S.events.get(r.id), true); }
  }
  renderPhones(); renderBoard(); renderChips();
}
let booted = false;
async function boot() {
  try { await refresh(); } catch (e) { return; }
  connectEvents(); route();
  if (!booted) { booted = true; thumbTick(); if (!S.settings.ready) setTimeout(() => toast('Tip: add the OpenRouter API key and management key in Settings (gear icon).'), 900); }
}
route();
boot();
