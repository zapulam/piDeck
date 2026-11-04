// piDeck UI: one websocket, swipeable pages, one alert overlay. No build step, no network deps.
const PAGES = ["agents", "now_playing", "slack"];
const VIEWS = {
  agents:      { label: "Agents",  render: renderAgents },
  now_playing: { label: "Playing", render: renderNow },
  slack:       { label: "Slack",   render: renderSlack },
  settings:    { label: "Settings", render: renderSettings },
};
const S = { spotify: null, spotifyAt: 0, slack: [], agents: [], agent_usage: {}, seenSlack: new Set(), unread: new Set(), cfg: { views: Object.keys(VIEWS), alert_seconds: 8, agents_stale_hours: 12 } };
let frame = 0;
const motions = new Map();
const completionSeen = (() => {
  try {
    const saved = JSON.parse(localStorage.getItem("pideck_completion_seen") || "{}");
    return new Map(Object.entries(saved).filter(([, at]) => Number.isFinite(at)));
  } catch { return new Map(); }
})();
function saveCompletionSeen() {
  try { localStorage.setItem("pideck_completion_seen", JSON.stringify(Object.fromEntries(completionSeen))); } catch {}
}
function acknowledgeSession(id) {
  const a = S.agents.find((agent) => agent.id === id);
  if (!a || a.state !== "done") return;
  completionSeen.set(id, a.state_since);
  saveCompletionSeen();
  const sec = $("#v-agents"); if (sec) renderAgents(sec);
}

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
  return [...PAGES.filter((id) => chosen.has(id)), "settings"];
}
function buildShell() {
  const track = $("#track"), pager = $("#pager");
  const keep = currentView();
  track.innerHTML = ""; pager.innerHTML = "";
  const pages = document.createElement("div");
  pages.id = "pager-pages";
  navViews().forEach((v) => {
    const sec = document.createElement("section");
    sec.id = "v-" + v; sec.dataset.view = v;
    track.appendChild(sec);
    const b = document.createElement("button");
    b.type = "button";
    if (v === "settings") b.id = "settings-btn";
    b.dataset.view = v; b.textContent = VIEWS[v].label;
    b.onclick = () => sec.scrollIntoView({ behavior: "smooth", inline: "start" });
    pages.appendChild(b);
  });
  pager.appendChild(pages);
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
    const t = $("#track"); if (touching || drag || !t.clientWidth) return;
    const i = Math.round(t.scrollLeft / t.clientWidth), target = i * t.clientWidth;
    if (Math.abs(t.scrollLeft - target) > 2) t.scrollTo({ left: target, behavior: "smooth" });
  }, 160);
}
function markPager() {
  const track = $("#track");
  const i = Math.round(track.scrollLeft / track.clientWidth);
  [...$("#pager-pages").children].forEach((b, j) => b.classList.toggle("on", i === j));
  const cur = track.children[i]?.dataset.view;
  setBg();
  if (cur === "slack" && S.unread.size) { S.unread.clear(); renderAll(); }
}
function currentView() {
  const t = $("#track"); return t.children[Math.round(t.scrollLeft / t.clientWidth)]?.dataset.view;
}

// ---------- renderers ----------
function renderAll() {
  if (drag) { pendingRender = true; return; }
  for (const sec of $("#track").children) VIEWS[sec.dataset.view].render(sec);
  badges();
}

const ORDER = { waiting: 0, error: 1, working: 2, done: 3, idle: 4 };
const PROVIDERS = { claude: "Claude", codex: "Codex", cursor: "Cursor" };
const GROUPS = ["cursor", "claude", "codex"];
const DOCKS = ["top", "bottom", "left", "right"];
const layout = (() => {
  let saved;
  try { saved = JSON.parse(localStorage.getItem("pideck_agent_layout") || "{}"); } catch {}
  return {
    groups: [...new Set([...(Array.isArray(saved?.groups) ? saved.groups.filter((v) => GROUPS.includes(v)) : []), ...GROUPS])],
    dock: DOCKS.includes(saved?.dock) ? saved.dock : "bottom",
  };
})();
function saveLayout() {
  try { localStorage.setItem("pideck_agent_layout", JSON.stringify(layout)); } catch {}
}
function sessionTag(id) {
  const raw = String(id || "").replace(/^[^:]*:/, "").replace(/[^a-z0-9]/gi, "");
  return raw.slice(-4).toUpperCase();
}
function tileHtml(a, peers) {
  const ctx = a.context_pct != null ? Math.round(a.context_pct) : null;
  const status = a.state === "working" ? "working" : a.state === "waiting" ? "needs you" : a.state;
  const same = peers.filter((b) => b.name === a.name);
  const parent = a.parent_id && S.agents.find((b) => b.id === a.parent_id);
  const bits = [];
  if (parent) bits.push("sub of " + parent.name);
  if (a.detail) bits.push(a.detail);
  if (same.length > 1) bits.push("#" + sessionTag(a.id));
  const mark = bits.join(" · ");
  const seed = same.length > 1 ? a.id : a.name;
  const unseen = a.state === "done" && completionSeen.get(a.id) !== a.state_since;
  return `<div class="tile" data-id="${esc(a.id)}" data-state="${a.state}" data-kind="${a.agent || "claude"}" data-name="${esc(a.name)}" data-seed="${esc(seed)}" style="--ctx:${ctx ?? 0}%"
    ${unseen ? `data-unseen="true" role="button" tabindex="0" aria-label="${esc(a.name)} completed. Mark as seen" title="Click to mark this session as seen"` : ""}>
    ${unseen ? `<span class="completion-badge" role="img" aria-label="Unseen completion">✓</span>` : ""}
    <div class="sprite"><canvas width="12" height="12"></canvas><span class="thinking" role="img" aria-label="Thinking" hidden><i></i><i></i><i></i></span></div>
    <div class="name">${esc(a.name)}</div>
    ${mark ? `<div class="tag">${esc(mark)}</div>` : ""}
    <div class="meta"><b>${status}</b></div>
    <div class="context-label">${a.agent === "cursor" && ctx != null ? "Before compact" : "Context"} ${ctx != null ? ctx + "%" : "unavailable"}</div>
    ${ctx == null ? "" : `<div class="context-track" role="progressbar" aria-label="${esc(a.name)} context used" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${ctx}"><div></div></div>`}
    <div class="meta" data-ago="${a.state_since}">${ago(a.state_since)}</div>
  </div>`;
}
function usageDuration(minutes) {
  return minutes === 10080 ? "Weekly" : minutes % 1440 === 0 ? `${minutes / 1440}d` :
    minutes % 60 === 0 ? `${minutes / 60}h` : `${minutes}m`;
}
function renderUsage(list) {
  const now = Date.now() / 1000;
  const meters = layout.groups.map((kind) => [kind, PROVIDERS[kind]]).filter(([kind]) =>
    list.some((a) => (a.agent || "claude") === kind) ||
    now - (S.agent_usage[kind]?.updated_at || 0) < S.cfg.agents_stale_hours * 3600
  ).map(([kind, name]) => {
    const usage = S.agent_usage[kind];
    const stale = !usage || now - usage.updated_at >= S.cfg.agents_stale_hours * 3600;
    const rings = ["primary", "secondary"].map((key, i) => {
      const win = usage?.[key];
      const expired = win?.resets_at != null && win.resets_at <= now;
      const pct = !stale && !expired && win && Number.isFinite(win.used_pct) ? Math.round(win.used_pct) : null;
      const label = win?.label || (kind === "cursor" ? (i ? "API" : "Included") : win ? usageDuration(win.window_minutes) : i ? "Weekly" : "5h");
      const detail = pct == null ? "—" : pct + "%";
      const level = pct == null ? "unknown" : pct < 70 ? "green" : pct <= 90 ? "yellow" : "red";
      return `<div class="usage-metric" title="${esc(name + " " + label + ": " + (pct == null ? "unavailable" : detail + " used"))}">
        <div class="usage-donut" data-level="${level}" style="--used:${pct ?? 0}%" ${pct == null ?
          `role="img" aria-label="${esc(name + " " + label)} usage unavailable"` :
          `role="progressbar" aria-label="${esc(name + " " + label)} account usage" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"`}>
          <b>${detail}</b></div><span>${esc(label)}</span></div>`;
    }).join("");
    return `<div class="usage-meter" data-provider="${kind}"><b>${name}</b><div class="usage-pair">${rings}</div></div>`;
  });
  return meters.length ? `<aside class="usage-dock" data-drag-usage aria-label="Account usage">
    <button type="button" class="usage-grab" data-drag-usage aria-label="Move usage panel" title="Drag to an edge, or use the arrow keys"><span class="drag-grip" aria-hidden="true"></span></button>
    ${meters.join("")}</aside>` : "";
}
function renderAgents(sec) {
  if (drag) { pendingRender = true; return; }
  const scroll = $(".agents-main", sec)?.scrollTop || 0;
  const list = [...S.agents];
  const w = list.filter((a) => a.state === "working").length, n = list.filter((a) => a.state === "waiting").length;
  let html = `<h2>Agents <small>${w} working${n ? ` · <span style="color:var(--waiting)">${n} waiting</span>` : ""}</small></h2>`;
  if (!list.length) {
    html += `<div class="empty">No agents yet.<br>Start a Claude Code, Codex, or Cursor session with the piDeck hook installed<br>(see <code>hooks/</code>) and it will show up here.</div>`;
  } else {
    const kinds = [...layout.groups];
    for (const a of list) {
      const kind = a.agent || "claude";
      if (!kinds.includes(kind)) kinds.push(kind);
    }
    html += kinds.map((kind) => {
      const items = list.filter((a) => (a.agent || "claude") === kind)
        .sort((a, b) => (a.parent_id || a.id).localeCompare(b.parent_id || b.id)
          || (a.parent_id ? 1 : 0) - (b.parent_id ? 1 : 0)
          || ORDER[a.state] - ORDER[b.state] || b.updated_at - a.updated_at);
      if (!items.length) return "";
      const label = PROVIDERS[kind] || kind;
      return `<div class="agent-group" data-provider="${esc(kind)}"><h3><button type="button" class="group-handle" data-drag-group="${esc(kind)}"
        aria-label="Move ${esc(label)} group" title="Drag to reorder, or use the up and down arrow keys"><span class="drag-grip" aria-hidden="true"></span>${esc(label)}</button></h3>
        <div class="grid">${items.map((a) => tileHtml(a, items)).join("")}</div></div>`;
    }).join("");
  }
  sec.classList.add("agents-page");
  sec.dataset.dock = layout.dock;
  sec.innerHTML = `<div class="agents-main">${html}</div>${renderUsage(list)}`;
  $(".agents-main", sec).scrollTop = scroll;
  animateTiles();
}
function animateTiles() {
  const now = performance.now();
  document.querySelectorAll(".tile").forEach((t) => {
    let motion = motions.get(t.dataset.id);
    if (!motion || motion.state !== t.dataset.state) {
      motion = creatureMotion(t.dataset.state, now);
      motions.set(t.dataset.id, motion);
    }
    const pose = stepCreatureMotion(motion, now);
    $(".thinking", t).hidden = !pose.thinking;
    drawCreature($("canvas", t), { state: t.dataset.state, name: t.dataset.name, seed: t.dataset.seed, frame, ...pose });
  });
}

// ---------- display layout: pointer dragging and keyboard alternatives ----------
let drag = null, dragFrame = null, pendingRender = false, suppressClickUntil = 0;
function stagePoint({ clientX, clientY }) {
  const stage = $("#stage"), r = stage.getBoundingClientRect();
  switch (+stage.dataset.rot) {
    case 90: return { x: clientY - r.top, y: r.right - clientX };
    case 180: return { x: r.right - clientX, y: r.bottom - clientY };
    case 270: return { x: r.bottom - clientY, y: clientX - r.left };
    default: return { x: clientX - r.left, y: clientY - r.top };
  }
}
function stageBounds(el) {
  const r = el.getBoundingClientRect();
  const a = stagePoint({ clientX: r.left, clientY: r.top });
  const b = stagePoint({ clientX: r.right, clientY: r.bottom });
  return { left: Math.min(a.x, b.x), right: Math.max(a.x, b.x),
    top: Math.min(a.y, b.y), bottom: Math.max(a.y, b.y) };
}
function moveGroup(kind, before) {
  const order = layout.groups.filter((id) => id !== kind);
  const index = before == null ? order.length : order.indexOf(before);
  order.splice(index < 0 ? order.length : index, 0, kind);
  layout.groups = order;
  saveLayout();
}
function updateDrag() {
  if (!drag?.active) return;
  const sec = $("#v-agents"), p = stagePoint(drag), r = stageBounds(sec);
  drag.inside = p.x >= r.left && p.x <= r.right && p.y >= r.top && p.y <= r.bottom;
  drag.preview.style.left = drag.clientX + 12 + "px";
  drag.preview.style.top = drag.clientY + 12 + "px";
  sec.querySelectorAll(".drop-before, .drop-after").forEach((el) => el.classList.remove("drop-before", "drop-after"));
  if (drag.kind === "usage") {
    const distances = { top: p.y - r.top, bottom: r.bottom - p.y, left: p.x - r.left, right: r.right - p.x };
    drag.dock = DOCKS.reduce((best, edge) => distances[edge] < distances[best] ? edge : best, DOCKS[0]);
    sec.querySelectorAll("[data-drop-dock]").forEach((el) => {
      el.classList.toggle("chosen", drag.inside && el.dataset.dropDock === drag.dock);
    });
  } else if (drag.inside) {
    const groups = [...sec.querySelectorAll(".agent-group")].filter((el) => el.dataset.provider !== drag.kind);
    const next = groups.find((el) => { const box = stageBounds(el); return p.y < (box.top + box.bottom) / 2; });
    drag.before = next?.dataset.provider ?? null;
    (next || groups[groups.length - 1])?.classList.add(next ? "drop-before" : "drop-after");
  }
}
function dragTick() {
  if (!drag?.active) return;
  if (drag.kind !== "usage") {
    const main = $("#v-agents .agents-main"), p = stagePoint(drag), r = stageBounds(main);
    if (p.x >= r.left && p.x <= r.right && p.y >= r.top - 24 && p.y <= r.bottom + 24) {
      main.scrollTop += p.y < r.top + 30 ? -12 : p.y > r.bottom - 30 ? 12 : 0;
    }
  }
  updateDrag();
  dragFrame = requestAnimationFrame(dragTick);
}
function finishDrag(cancel = false) {
  if (!drag) return;
  updateDrag();
  const finished = drag;
  drag = null;
  cancelAnimationFrame(dragFrame);
  finished.preview?.remove();
  $("#v-agents .dock-targets")?.remove();
  document.querySelectorAll(".dragging, .drop-before, .drop-after").forEach((el) => el.classList.remove("dragging", "drop-before", "drop-after"));
  if (finished.active) {
    suppressClickUntil = performance.now() + 250;
    if (!cancel && finished.inside) {
      if (finished.kind === "usage") { layout.dock = finished.dock; saveLayout(); }
      else moveGroup(finished.kind, finished.before);
    }
  }
  try { finished.handle.releasePointerCapture(finished.pointerId); } catch {}
  if (finished.active || pendingRender) {
    pendingRender = false;
    renderAll();
    if (finished.kind !== "usage") {
      [...document.querySelectorAll("[data-drag-group]")].find((el) => el.dataset.dragGroup === finished.kind)?.focus({ preventScroll: true });
    }
  }
}
document.addEventListener("pointerdown", (e) => {
  const handle = e.target.closest("[data-drag-group], [data-drag-usage]");
  if (!handle || e.button !== 0 || drag) return;
  drag = { handle, kind: handle.dataset.dragGroup || "usage", pointerId: e.pointerId,
    startX: e.clientX, startY: e.clientY, clientX: e.clientX, clientY: e.clientY, active: false };
  handle.setPointerCapture(e.pointerId);
});
document.addEventListener("pointermove", (e) => {
  if (!drag || drag.pointerId !== e.pointerId) return;
  drag.clientX = e.clientX; drag.clientY = e.clientY;
  if (!drag.active && Math.hypot(e.clientX - drag.startX, e.clientY - drag.startY) > 5) {
    drag.active = true;
    const source = drag.handle.closest(drag.kind === "usage" ? ".usage-dock" : ".agent-group");
    source.classList.add("dragging");
    drag.preview = document.createElement("div");
    drag.preview.className = "drag-preview";
    drag.preview.textContent = drag.kind === "usage" ? "Account usage" : PROVIDERS[drag.kind] || drag.kind;
    document.body.appendChild(drag.preview);
    if (drag.kind === "usage") {
      $("#v-agents").insertAdjacentHTML("beforeend", `<div class="dock-targets" aria-hidden="true">${DOCKS.map((edge) =>
        `<span class="dock-target" data-drop-dock="${edge}">${edge}</span>`).join("")}</div>`);
    }
    dragTick();
  }
  if (drag.active) { e.preventDefault(); updateDrag(); }
});
document.addEventListener("pointerup", (e) => { if (drag?.pointerId === e.pointerId) finishDrag(); });
document.addEventListener("pointercancel", (e) => { if (drag?.pointerId === e.pointerId) finishDrag(true); });
document.addEventListener("lostpointercapture", (e) => { if (drag?.pointerId === e.pointerId) finishDrag(true); });

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
  const key = JSON.stringify([S.cfg.views, layout.dock]);
  if (sec.dataset.selection === key) return;
  sec.dataset.selection = key;
  sec.innerHTML = `<h2>Settings</h2>
    <p class="settings-note">Choose which pages appear in the bar. Settings always stays available.</p>` +
    PAGES.map((id) => {
      const on = (S.cfg.views || PAGES).includes(id);
      return `<button type="button" class="setting-row" data-page="${id}" aria-pressed="${on}">
        <span>${VIEWS[id].label}</span><b>${on ? "On" : "Off"}</b></button>`;
    }).join("") + `<h3 class="settings-heading">Agent layout</h3>
    <p class="settings-note">Drag a provider heading to reorder its agents. Use the grip on the usage panel to move it to any edge. Your layout is saved on this display. Provider headings also support the up and down arrow keys.</p>
    <div class="dock-options" aria-label="Usage panel position">${DOCKS.map((dock) =>
      `<button type="button" data-dock="${dock}" aria-pressed="${layout.dock === dock}">${dock}</button>`).join("")}</div>
    <button type="button" class="reset-layout" data-reset-layout>Reset agent layout</button>`;
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
let alertTimer = null, alertFrame = null;
const alertQueue = [];
function envelope(canvas) {
  const c = canvas.getContext("2d"); c.clearRect(0, 0, 12, 12);
  c.fillStyle = "#ffc86b"; c.fillRect(1, 3, 10, 7);
  c.fillStyle = "#7a4d0e"; for (let i = 0; i < 5; i++) { c.fillRect(1 + i, 3 + i, 1, 1); c.fillRect(10 - i, 3 + i, 1, 1); }
}
function showAlert(a) {
  const el = $("#alert");
  if (!el.hidden) { alertQueue.push(a); return; }
  el.className = a.kind === "agent" ? "agent-" + a.state : a.kind;
  $("#alert-title").textContent = a.title;
  $("#alert-body").textContent = a.body || "";
  const cv = $("#alert-sprite");
  clearInterval(alertFrame);
  if (a.kind === "agent") {
    let f = 0;
    const name = a.title.replace(/ (finished|needs you)$/, "");
    const tile = S.agents.find((agent) => agent.id === a.session_id);
    const peers = S.agents.filter((agent) => agent.agent === tile?.agent && agent.name === tile?.name);
    const seed = tile && peers.length > 1 ? tile.id : name;
    const accessory = a.state === "done" ? ACCESSORIES[Math.floor(Math.random() * ACCESSORIES.length)] : undefined;
    cv.dataset.accessory = accessory || "";
    cv.dataset.sessionId = a.session_id || "";
    // Celebrate awake; the card returns to its resting pose after the takeover.
    const draw = () => drawCreature(cv, { state: a.state === "done" ? "celebrating" : a.state, name, seed, accessory, frame: f++ });
    draw(); alertFrame = setInterval(draw, 450);
  } else envelope(cv);
  el.hidden = false;
  clearTimeout(alertTimer);
  const seconds = a.seconds ?? S.cfg.alert_seconds;
  alertTimer = setTimeout(hideAlert, (a.state === "done" ? Math.min(seconds, 4) : seconds) * 1000);
}
function hideAlert() {
  $("#alert").hidden = true; clearInterval(alertFrame); clearTimeout(alertTimer);
  if (alertQueue.length) showAlert(alertQueue.shift());
}

// ---------- data ----------
function onState(key, value) {
  if (key === "spotify") { S.spotify = value; S.spotifyAt = performance.now(); }
  else if (key === "slack") {
    for (const m of value) if (!S.seenSlack.has(m.id)) { S.seenSlack.add(m.id); if (S.initialized && currentView() !== "slack") S.unread.add(m.id); }
    S.slack = value;
  } else {
    S[key] = value;
    if (key === "agents") {
      const ids = new Set(value.map((a) => a.id));
      for (const id of motions.keys()) if (!ids.has(id)) motions.delete(id);
      for (const id of completionSeen.keys()) {
        if (!value.some((a) => a.id === id && a.state === "done")) completionSeen.delete(id);
      }
      saveCompletionSeen();
    }
  }
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
  if (performance.now() < suppressClickUntil) { e.preventDefault(); return; }
  const dock = e.target.closest("button[data-dock]")?.dataset.dock;
  if (dock) { layout.dock = dock; saveLayout(); renderAll(); return; }
  if (e.target.closest("[data-reset-layout]")) {
    layout.groups = [...GROUPS]; layout.dock = "bottom"; saveLayout(); renderAll(); return;
  }
  const tile = e.target.closest('.tile[data-unseen="true"]');
  if (tile) return acknowledgeSession(tile.dataset.id);
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
  if (drag && e.key === "Escape") { e.preventDefault(); finishDrag(true); return; }
  const group = e.target.closest("[data-drag-group]");
  if (group && (e.key === "ArrowUp" || e.key === "ArrowDown")) {
    e.preventDefault();
    const shown = [...document.querySelectorAll(".agent-group")].map((el) => el.dataset.provider);
    const i = shown.indexOf(group.dataset.dragGroup);
    if (e.key === "ArrowUp" && i > 0) moveGroup(shown[i], shown[i - 1]);
    if (e.key === "ArrowDown" && i < shown.length - 1) moveGroup(shown[i], shown[i + 2] ?? null);
    renderAll();
    [...document.querySelectorAll("[data-drag-group]")].find((el) => el.dataset.dragGroup === group.dataset.dragGroup)?.focus();
    return;
  }
  if (e.target.closest("[data-drag-usage]") && e.key.startsWith("Arrow")) {
    e.preventDefault();
    const edge = { ArrowUp: "top", ArrowDown: "bottom", ArrowLeft: "left", ArrowRight: "right" }[e.key];
    if (edge) { layout.dock = edge; saveLayout(); renderAll(); $(".usage-grab")?.focus(); }
    return;
  }
  const tile = e.target.closest('.tile[data-unseen="true"]');
  if (tile && (e.key === "Enter" || e.key === " ")) {
    e.preventDefault(); acknowledgeSession(tile.dataset.id); return;
  }
  const t = $("#track");
  if (e.key === "ArrowRight") t.scrollBy({ left: t.clientWidth, behavior: "smooth" });
  if (e.key === "ArrowLeft") t.scrollBy({ left: -t.clientWidth, behavior: "smooth" });
  if (e.key === "Escape") {
    hideAlert();
  }
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
  setInterval(() => { frame = Math.floor(performance.now() / 450); animateTiles(); }, 100);
  setInterval(() => { tickProgress(); tickClock(); }, 500);
  setInterval(() => document.querySelectorAll("[data-ago]").forEach((el) => (el.textContent = ago(+el.dataset.ago))), 15000);
  setInterval(() => { const sec = $("#v-agents"); if (sec) renderAgents(sec); }, 15000);
  S.initialized = false;
})();
