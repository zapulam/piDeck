// piDeck UI: one websocket, three swipeable pages, one alert overlay. No build step, no network deps.
const PAGES = ["agents", "now_playing", "slack"];
const VIEWS = {
  agents:      { label: "Agents",  render: renderAgents },
  now_playing: { label: "Playing", render: renderNow },
  slack:       { label: "Slack",   render: renderSlack },
  settings:    { label: "Settings", render: renderSettings },
};
const S = { spotify: null, spotifyAt: 0, slack: [], agents: [], agent_usage: {}, seenSlack: new Set(), unread: new Set(), cfg: { views: Object.keys(VIEWS), alert_seconds: 8, agents_stale_hours: 12 } };
let frame = 0;

const $ = (sel, el = document) => el.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function ago(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 45) return "now";
  if (s < 3600) return Math.round(s / 60) + "m ago";
  if (s < 86400) return Math.round(s / 3600) + "h ago";
  return Math.round(s / 86400) + "d ago";
}
const mmss = (ms) => { const t = Math.floor(ms / 1000); return Math.floor(t / 60) + ":" + String(t % 60).padStart(2, "0"); };

// ---------- shell ----------
function navViews() {
  const chosen = new Set(S.cfg.views || PAGES);
  return PAGES.filter((id) => chosen.has(id)).concat(["settings"]);
}
function buildShell() {
  const track = $("#track"), pager = $("#pager");
  const keep = currentView();
  track.innerHTML = ""; pager.innerHTML = "";
  navViews().forEach((v) => {
    const sec = document.createElement("section");
    sec.id = "v-" + v; sec.dataset.view = v;
    track.appendChild(sec);
    const b = document.createElement("button");
    b.dataset.view = v; b.textContent = VIEWS[v].label;
    b.onclick = () => sec.scrollIntoView({ behavior: "smooth", inline: "start" });
    pager.appendChild(b);
  });
  if (!track.dataset.bound) {
    track.dataset.bound = "1";
    track.addEventListener("scroll", () => { requestAnimationFrame(markPager); settleSoon(); }, { passive: true });
    track.addEventListener("touchstart", () => { touching = true; }, { passive: true });
    const release = () => { touching = false; settleSoon(); };
    track.addEventListener("touchend", release, { passive: true });
    track.addEventListener("touchcancel", release, { passive: true });
  }
  const stay = [...track.children].find((sec) => sec.dataset.view === keep) || track.children[0];
  if (stay) stay.scrollIntoView({ inline: "start" });
  markPager();
  renderAll();
}
// CSS scroll-snap normally lands on a page; this is the safety net for when it doesn't (seen with
// software-rotated screens): once scrolling stops and no finger is down, glide to the nearest page.
let touching = false, settleTimer = null;
function settleSoon() {
  clearTimeout(settleTimer);
  settleTimer = setTimeout(() => {
    const t = $("#track"); if (touching || !t.clientWidth) return;
    const i = Math.round(t.scrollLeft / t.clientWidth), target = i * t.clientWidth;
    if (Math.abs(t.scrollLeft - target) > 2) t.scrollTo({ left: target, behavior: "smooth" });
  }, 160);
}
function markPager() {
  const track = $("#track");
  const i = Math.round(track.scrollLeft / track.clientWidth);
  [...$("#pager").children].forEach((b, j) => b.classList.toggle("on", i === j));
  const cur = track.children[i]?.dataset.view;
  setBg();
  if (cur === "slack" && S.unread.size) { S.unread.clear(); renderAll(); }
}
function currentView() {
  const t = $("#track"); return t.children[Math.round(t.scrollLeft / t.clientWidth)]?.dataset.view;
}

// ---------- renderers ----------
function renderAll() { for (const sec of $("#track").children) VIEWS[sec.dataset.view].render(sec); badges(); }

const ORDER = { waiting: 0, error: 1, working: 2, done: 3, idle: 4 };
const PROVIDERS = { claude: "Claude", codex: "Codex", cursor: "Cursor" };
function usageDuration(minutes) {
  return minutes === 10080 ? "Weekly" : minutes % 1440 === 0 ? `${minutes / 1440}d` :
    minutes % 60 === 0 ? `${minutes / 60}h` : `${minutes}m`;
}
function renderUsage(list) {
  const now = Date.now() / 1000;
  const meters = Object.entries(PROVIDERS).filter(([kind]) =>
    list.some((a) => (a.agent || "claude") === kind) ||
    now - (S.agent_usage[kind]?.updated_at || 0) < S.cfg.agents_stale_hours * 3600
  ).map(([kind, name]) => {
    if (kind === "cursor") return `<div class="usage-meter" data-provider="cursor"><b>Cursor</b>
      <div class="usage-unavailable">Account limits unavailable</div></div>`;
    const usage = S.agent_usage[kind];
    const stale = !usage || now - usage.updated_at >= S.cfg.agents_stale_hours * 3600;
    const bars = ["primary", "secondary"].map((key, i) => {
      const win = usage?.[key];
      const expired = win?.resets_at != null && win.resets_at <= now;
      const pct = !stale && !expired && win && Number.isFinite(win.used_pct) ? Math.round(win.used_pct) : null;
      const label = win ? usageDuration(win.window_minutes) : i ? "Weekly" : "5h";
      const detail = pct == null ? "—" : pct + "%";
      return `<div class="usage-label"><span>${label}</span><b>${detail}</b></div>
        <div class="usage-track ${pct == null ? "unknown" : ""}" ${pct == null ? "" :
          `role="progressbar" aria-label="${name} ${label} account usage" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"`}
          style="--used:${pct ?? 0}%;--usage-color:var(${pct >= 90 ? "--error" : pct >= 75 ? "--working" : "--done"})"><div></div></div>`;
    }).join("");
    return `<div class="usage-meter" data-provider="${kind}"><b>${name}</b><div class="usage-pair">${bars}</div></div>`;
  });
  return meters.length ? `<div class="usage-dock">${meters.join("")}</div>` : "";
}
function renderAgents(sec) {
  const list = [...S.agents].sort((a, b) => ORDER[a.state] - ORDER[b.state] || b.updated_at - a.updated_at);
  const w = list.filter((a) => a.state === "working").length, n = list.filter((a) => a.state === "waiting").length;
  let html = `<h2>Agents <small>${w} working${n ? ` · <span style="color:var(--waiting)">${n} waiting</span>` : ""}</small></h2>`;
  if (!list.length) {
    html += `<div class="empty">No agents yet.<br>Start a Claude Code, Codex, or Cursor session with the piDeck hook installed<br>(see <code>hooks/</code>) and it will show up here.</div>`;
  } else {
    html += `<div class="grid">` + list.map((a) => {
      const ctx = a.context_pct != null ? Math.round(a.context_pct) : null;
      const status = a.state === "working" ? "working" : a.state === "waiting" ? "needs you" : a.state;
      return `<div class="tile" data-id="${esc(a.id)}" data-state="${a.state}" data-kind="${a.agent || "claude"}" data-name="${esc(a.name)}" style="--ctx:${ctx ?? 0}%">
        <canvas width="12" height="12"></canvas>
        <div class="name">${esc(a.name)}</div>
        <div class="meta"><b>${status}</b> · ${PROVIDERS[a.agent] || "Claude"}</div>
        <div class="context-label">${a.agent === "cursor" && ctx != null ? "Before compact" : "Context"} ${ctx != null ? ctx + "%" : "unavailable"}</div>
        ${ctx == null ? "" : `<div class="context-track" role="progressbar" aria-label="${esc(a.name)} context used" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${ctx}"><div></div></div>`}
        <div class="meta" data-ago="${a.state_since}">${ago(a.state_since)}</div>
      </div>`;
    }).join("") + `</div>`;
  }
  sec.classList.add("agents-page");
  sec.innerHTML = `<div class="agents-main">${html}</div>${renderUsage(list)}`;
  animateTiles();
}
function animateTiles() {
  document.querySelectorAll(".tile").forEach((t) =>
    drawCreature($("canvas", t), { state: t.dataset.state, name: t.dataset.name, kind: t.dataset.kind, frame }));
}

let npDom = null;
function renderNow(sec) {
  const sp = S.spotify;
  if (!sp) {
    const d = new Date();
    sec.innerHTML = `<div class="np"><div class="clock" id="clock"></div><div class="date" id="date"></div><div class="date">nothing playing</div></div>`;
    npDom = null; tickClock(); setBg(null); return;
  }
  sec.innerHTML = `<div class="np">
    <div class="art" style="background-image:url('${esc(sp.art || "")}')"></div>
    <div class="info">
    <div class="title">${esc(sp.title)}</div>
    <div class="artist">${esc(sp.artist)}</div>
    <div class="bar"><div class="track"><div class="fill" id="fill"></div></div>
      <div class="times"><span id="t-now">0:00</span><span>${mmss(sp.duration_ms)}</span></div></div>
    <div class="ctl">
      <button data-act="previous" aria-label="previous">⏮</button>
      <button class="big" data-act="${sp.is_playing ? "pause" : "play"}" aria-label="play/pause">${sp.is_playing ? "⏸" : "▶"}</button>
      <button data-act="next" aria-label="next">⏭</button>
    </div></div></div>`;
  npDom = { fill: $("#fill", sec), now: $("#t-now", sec) };
  setBg(sp.art); tickProgress();
}
let bgUrl = null;
function setBg(url) {
  if (url !== undefined) bgUrl = url;
  const bg = $("#bgart");
  if (bgUrl) bg.style.backgroundImage = `url('${bgUrl}')`;
  // the album-art wash only belongs behind the Now Playing page
  bg.classList.toggle("on", !!bgUrl && currentView() === "now_playing");
}
function tickProgress() {
  const sp = S.spotify; if (!sp || !npDom) return;
  const ms = Math.min(sp.duration_ms, sp.progress_ms + (sp.is_playing ? performance.now() - S.spotifyAt : 0));
  npDom.fill.style.width = (sp.duration_ms ? (ms / sp.duration_ms) * 100 : 0) + "%";
  npDom.now.textContent = mmss(ms);
}
function tickClock() {
  const c = $("#clock"); if (!c) return;
  const d = new Date();
  c.textContent = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  $("#date").textContent = d.toLocaleDateString([], { weekday: "long", month: "short", day: "numeric" });
}

function renderSettings(sec) {
  sec.innerHTML = `<h2>Settings</h2>
    <p class="settings-note">Choose which pages appear in the bar below. Settings stays available.</p>` +
    PAGES.map((id) => {
      const on = (S.cfg.views || PAGES).includes(id);
      return `<button type="button" class="setting-row" data-page="${id}" aria-pressed="${on}">
        <span>${VIEWS[id].label}</span><b>${on ? "On" : "Off"}</b></button>`;
    }).join("");
}
function renderSlack(sec) {
  let html = `<h2>Slack <small>${S.slack.length ? `<button class="clear" id="clear">clear</button>` : ""}</small></h2>`;
  if (!S.slack.length) html += `<div class="empty">All quiet.<br>DMs and @mentions land here.</div>`;
  html += S.slack.map((m) => `<div class="msg ${S.unread.has(m.id) ? "unread" : ""}" data-kind="${m.kind}">
      <div class="top"><b>${esc(m.user)}</b><span>${esc(m.channel)} · ${ago(m.ts)}</span></div>
      <div class="text">${esc(m.text)}</div></div>`).join("");
  sec.innerHTML = html;
}
function badges() {
  const b = $('#pager button[data-view="slack"]'); if (!b) return;
  $(".badge", b)?.remove();
  if (S.unread.size && currentView() !== "slack") b.insertAdjacentHTML("beforeend", `<span class="badge">${S.unread.size}</span>`);
}

// ---------- alerts ----------
let alertTimer = null, alertFrame = null, doneSeq = 0;
const doneAlerts = [];
function envelope(canvas) {
  const c = canvas.getContext("2d"); c.clearRect(0, 0, 12, 12);
  c.fillStyle = "#ffc86b"; c.fillRect(1, 3, 10, 7);
  c.fillStyle = "#7a4d0e"; for (let i = 0; i < 5; i++) { c.fillRect(1 + i, 3 + i, 1, 1); c.fillRect(10 - i, 3 + i, 1, 1); }
}
function renderDoneStack() {
  const root = $("#done-stack");
  root.innerHTML = doneAlerts.map((a, i) => {
    const depth = doneAlerts.length - 1 - i;
    const rot = ((i % 2 ? 1 : -1) * (4 + depth * 3)) + "deg";
    const x = (depth * -10) + "px";
    const y = (depth * -16) + "px";
    return `<button type="button" class="done-card" data-id="${a.id}" data-name="${esc(a.name)}" data-kind="${a.agent || "claude"}"
      style="--rot:${rot};--x:${x};--y:${y};z-index:${i + 1}">
      <canvas width="12" height="12"></canvas>
      <div><b>${esc(a.title)}</b><span>click to dismiss</span></div>
    </button>`;
  }).join("");
  animateDoneStack();
}
function animateDoneStack() {
  document.querySelectorAll(".done-card").forEach((card) =>
    drawCreature($("canvas", card), { state: "done", name: card.dataset.name, kind: card.dataset.kind, frame }));
}
function dismissDone(id) {
  const index = doneAlerts.findIndex((a) => a.id === id);
  if (index >= 0) doneAlerts.splice(index, 1);
  renderDoneStack();
}
function showAlert(a) {
  if (a.kind === "agent" && a.state === "done") {
    doneAlerts.push({ ...a, id: ++doneSeq, name: (a.title || "").replace(/ finished$/, "") });
    renderDoneStack();
    return;
  }
  const el = $("#alert");
  el.className = a.kind === "agent" ? "agent-" + a.state : a.kind;
  $("#alert-title").textContent = a.title;
  $("#alert-body").textContent = a.body || "";
  const cv = $("#alert-sprite");
  clearInterval(alertFrame);
  if (a.kind === "agent") {
    let f = 0; const name = a.title.replace(/ (finished|needs you)$/, "");
    const draw = () => drawCreature(cv, { state: a.state, name, kind: a.agent || "claude", frame: f++ });
    draw(); alertFrame = setInterval(draw, 450);
  } else envelope(cv);
  el.hidden = false;
  clearTimeout(alertTimer);
  alertTimer = setTimeout(hideAlert, (a.seconds || S.cfg.alert_seconds) * 1000);
}
function hideAlert() { $("#alert").hidden = true; clearInterval(alertFrame); clearTimeout(alertTimer); }

// ---------- data ----------
function onState(key, value) {
  if (key === "spotify") { S.spotify = value; S.spotifyAt = performance.now(); }
  else if (key === "slack") {
    for (const m of value) if (!S.seenSlack.has(m.id)) { S.seenSlack.add(m.id); if (S.initialized && currentView() !== "slack") S.unread.add(m.id); }
    S.slack = value;
  } else S[key] = value;
  renderAll();
}
// The kiosk on the Pi needs no token. Another device can open  http://PI:8765/?token=...  once; it is
// remembered in that browser and removed from the address bar.
const TOKEN = (() => {
  let t = new URLSearchParams(location.search).get("token") || "";
  try {
    if (t) { localStorage.setItem("pideck_token", t); history.replaceState(null, "", location.pathname + location.hash); }
    else t = localStorage.getItem("pideck_token") || "";
  } catch {}
  return t;
})();
const authInit = (init = {}) => {
  const headers = { ...(init.headers || {}) };
  if (TOKEN) headers.Authorization = "Bearer " + TOKEN;
  return { ...init, headers };
};

function connect() {
  const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws" +
    (TOKEN ? "?token=" + encodeURIComponent(TOKEN) : ""));
  ws.onopen = () => { $("#conn").classList.remove("down"); connect.delay = 500; };
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type === "snapshot") {
      for (const k of Object.keys(m.state)) onState(k, m.state[k]);
      S.initialized = true;
    } else if (m.type === "state") onState(m.key, m.value);
    else if (m.type === "alert") showAlert(m.alert);
  };
  ws.onclose = () => {
    $("#conn").classList.add("down");
    setTimeout(connect, connect.delay = Math.min((connect.delay || 500) * 1.6, 8000));
  };
}

// ---------- boot ----------
document.addEventListener("click", async (e) => {
  const done = e.target.closest(".done-card");
  if (done) return dismissDone(+done.dataset.id);
  if (e.target.closest("#alert")) return hideAlert();
  const act = e.target.closest("[data-act]")?.dataset.act;
  if (act) {
    try { const r = await fetch("/api/spotify/" + act, authInit({ method: "POST" }));
      if (r.status === 403) alert403(); } catch {}
  }
  if (e.target.id === "clear") fetch("/api/slack/clear", authInit({ method: "POST" }));
  const page = e.target.closest("[data-page]")?.dataset.page;
  if (!page) return;
  const chosen = new Set(S.cfg.views || PAGES);
  chosen.has(page) ? chosen.delete(page) : chosen.add(page);
  const views = PAGES.filter((id) => chosen.has(id));
  try {
    const r = await fetch("/api/ui-settings", authInit({
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ views }),
    }));
    if (!r.ok) return;
    S.cfg.views = (await r.json()).views;
    buildShell();
  } catch {}
});
function alert403() { showAlert({ kind: "slack", title: "Spotify Premium needed", body: "Playback controls only work on Premium accounts.", seconds: 4 }); }
document.addEventListener("keydown", (e) => {
  const t = $("#track");
  if (e.key === "ArrowRight") t.scrollBy({ left: t.clientWidth, behavior: "smooth" });
  if (e.key === "ArrowLeft") t.scrollBy({ left: -t.clientWidth, behavior: "smooth" });
  if (e.key === "Escape") doneAlerts.length ? dismissDone(doneAlerts[doneAlerts.length - 1].id) : hideAlert();
});

// Rotate the whole UI in software (0/90/180/270). Prefer rotating the display in the OS; this is the
// fallback. Set `ui.rotate` in config.yaml, or try it live with ?rotate=90 in the URL.
function applyRotate() {
  const q = new URLSearchParams(location.search).get("rotate");
  const deg = [0, 90, 180, 270].includes(+q) && q !== null ? +q : +S.cfg.rotate || 0;
  $("#stage").dataset.rot = deg;
}

(async function boot() {
  try { Object.assign(S.cfg, await (await fetch("/api/config")).json()); } catch {}
  applyRotate();
  buildShell();
  renderAll();
  connect();
  setInterval(() => { frame++; animateTiles(); animateDoneStack(); }, 450);
  setInterval(() => { tickProgress(); tickClock(); }, 500);
  setInterval(() => document.querySelectorAll("[data-ago]").forEach((el) => (el.textContent = ago(+el.dataset.ago))), 15000);
  setInterval(() => { const sec = $("#v-agents"); if (sec) renderAgents(sec); }, 15000);
  S.initialized = false;
})();
