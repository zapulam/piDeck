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
const GEAR = `<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path fill="currentColor" d="M19.14 12.94c.04-.31.06-.63.06-.94s-.02-.63-.06-.94l2.03-1.58a.5.5 0 0 0 .12-.64l-1.92-3.32a.5.5 0 0 0-.6-.22l-2.39.96a7.1 7.1 0 0 0-1.63-.94l-.36-2.54a.5.5 0 0 0-.5-.42h-3.84a.5.5 0 0 0-.5.42l-.36 2.54c-.58.22-1.12.53-1.63.94l-2.39-.96a.5.5 0 0 0-.6.22L2.71 8.84a.5.5 0 0 0 .12.64l2.03 1.58c-.04.31-.06.63-.06.94s.02.63.06.94l-2.03 1.58a.5.5 0 0 0-.12.64l1.92 3.32c.13.23.4.32.6.22l2.39-.96c.51.41 1.05.72 1.63.94l.36 2.54c.05.24.26.42.5.42h3.84c.24 0 .45-.18.5-.42l.36-2.54c.58-.22 1.12-.53 1.63-.94l2.39.96c.22.1.47 0 .6-.22l1.92-3.32a.5.5 0 0 0-.12-.64l-2.03-1.58zM12 15.5A3.5 3.5 0 1 1 12 8.5a3.5 3.5 0 0 1 0 7z"/></svg>`;
function navViews() {
  const chosen = new Set(S.cfg.views || PAGES);
  return ["settings", ...PAGES.filter((id) => chosen.has(id))];
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
    if (v === "settings") {
      b.id = "settings-btn";
      b.setAttribute("aria-label", "Settings");
      b.title = "Settings";
      b.innerHTML = GEAR;
    } else b.textContent = VIEWS[v].label;
    b.dataset.view = v;
    b.onclick = () => sec.scrollIntoView({ behavior: "smooth", inline: "start" });
    if (v === "settings") pager.appendChild(b); else pages.appendChild(b);
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
  const stay = [...track.children].find((sec) => sec.dataset.view === keep) ||
    [...track.children].find((sec) => sec.dataset.view !== "settings") || track.children[0];
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
  const cur = track.children[i]?.dataset.view;
  $("#pager").querySelectorAll("[data-view]").forEach((b) => b.classList.toggle("on", b.dataset.view === cur));
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
let agentView = "agents";
let selectedUsage = null;
const agentScroll = { agents: 0, usage: 0, mini: 0 };
const layout = (() => {
  let saved;
  try { saved = JSON.parse(localStorage.getItem("pideck_agent_layout") || "{}"); } catch {}
  return {
    groups: [...new Set([...(Array.isArray(saved?.groups) ? saved.groups.filter((v) => GROUPS.includes(v)) : []), ...GROUPS])],
    version: 2,
    dock: saved?.version === 2 && DOCKS.includes(saved.dock) ? saved.dock : "right",
  };
})();
function providerLogo(kind) {
  return Object.hasOwn(PROVIDERS, kind) ?
    `<img class="provider-logo${kind === "claude" ? "" : " monochrome"}" src="logos/${kind}.svg" alt="" aria-hidden="true" draggable="false" width="18" height="18">` :
    `<span class="provider-fallback" aria-hidden="true">${esc(kind.slice(0, 1).toUpperCase())}</span>`;
}
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
    <div class="context-label">${a.agent === "cursor" && ctx != null ? "Before compact" : "Context"}${ctx == null ? " unavailable" : ""}</div>
    ${ctx == null ? "" : `<div class="context-track" role="progressbar" aria-label="${esc(a.name)} context used" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${ctx}"><div></div></div>`}
  </div>`;
}
function usageDuration(minutes) {
  return minutes === 10080 ? "Weekly" : minutes % 1440 === 0 ? `${minutes / 1440}d` :
    minutes % 60 === 0 ? `${minutes / 60}h` : `${minutes}m`;
}
function usageColor(pct) {
  const stops = [[95, 211, 176], [255, 210, 103], [255, 64, 64]];
  const low = pct <= 70 ? 0 : 1, fraction = pct <= 70 ? pct / 70 : (pct - 70) / 30;
  return `rgb(${stops[low].map((value, i) => Math.round(value + (stops[low + 1][i] - value) * fraction)).join(", ")})`;
}
function usageProviders(list) {
  const now = Date.now() / 1000;
  return layout.groups.filter((kind) =>
    list.some((a) => (a.agent || "claude") === kind) ||
    now - (S.agent_usage[kind]?.updated_at || 0) < S.cfg.agents_stale_hours * 3600
  ).map((kind) => {
    const usage = S.agent_usage[kind];
    const stale = !usage || now - usage.updated_at >= S.cfg.agents_stale_hours * 3600;
    const keys = kind === "cursor" ? ["secondary", "primary"] : ["primary", "secondary"];
    const windows = keys.map((key) => {
      const win = usage?.[key];
      const expired = win?.resets_at != null && win.resets_at <= now;
      const pct = !stale && !expired && win && Number.isFinite(win.used_pct) ? Math.max(0, Math.min(100, win.used_pct)) : null;
      const label = win?.label || (kind === "cursor" ? (key === "primary" ? "Included" : "API") :
        win ? usageDuration(win.window_minutes) : key === "secondary" ? "Weekly" : "5h");
      return { key, label, pct, resets: win?.resets_at, stale, expired };
    });
    return { kind, name: PROVIDERS[kind], windows };
  });
}
function usageBar(window, name) {
  return `<div class="usage-bar" data-window="${window.key}" data-available="${window.pct != null}"
    style="--used:${window.pct ?? 0}%;--usage-color:${window.pct == null ? "var(--dim)" : usageColor(window.pct)}" ${window.pct == null ?
      `role="img" aria-label="${esc(name + " " + window.label)} usage unavailable"` :
      `role="progressbar" aria-label="${esc(name + " " + window.label)} account usage" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${window.pct}"`}><div></div></div>`;
}
function usageReset(window) {
  if (window.pct == null) return window.expired ? "Awaiting a new usage sample" : "Usage unavailable";
  const seconds = window.resets - Date.now() / 1000;
  let reset = "Reset time unavailable";
  if (Number.isFinite(seconds) && seconds > 0) {
    const minutes = Math.ceil(seconds / 60), hours = Math.floor(minutes / 60), days = Math.floor(hours / 24);
    reset = "Resets in " + (days ? `${days}d ${hours % 24}h` : hours ? `${hours}h ${minutes % 60}m` : `${minutes}m`);
  }
  return (window.pct >= 100 ? "Limit reached · " : "") + reset;
}
function renderUsage(list, detailed = false) {
  const providers = usageProviders(list);
  if (detailed) return providers.length ? providers.map(({ kind, name, windows }) =>
    `<div class="usage-provider" data-provider="${kind}" data-selected="${selectedUsage === kind}" role="group" aria-label="${name} usage">
      <div class="usage-provider-logo" title="${name}">${providerLogo(kind)}</div><div class="usage-windows">${windows.map((window) =>
        `<div class="usage-window" data-window="${window.key}"><div class="usage-window-heading"><span>${esc(window.label === "5h" ? "5-hour session" : window.label)}</span>
          <b>${window.pct == null ? "—" : Math.round(window.pct) + "%"}</b></div>${usageBar(window, name)}
          <small>${esc(usageReset(window))}</small></div>`).join("")}</div></div>`).join("") :
    `<div class="empty">No account usage yet.<br>Usage appears when a provider reports its allowance.</div>`;
  return providers.length ? `<aside class="usage-dock" data-drag-usage aria-label="Account usage">
    <button type="button" class="usage-grab" data-drag-usage aria-label="Move usage panel" title="Drag to an edge, or use the arrow keys"><span class="drag-grip" aria-hidden="true"></span></button>
    ${providers.map(({ kind, name, windows }) => `<button type="button" class="usage-meter" data-provider="${kind}" data-show-usage="${kind}"
      aria-label="Show ${name} usage details" title="${name} usage">${providerLogo(kind)}<span class="usage-pair">${windows.map((window) => usageBar(window, name)).join("")}</span></button>`).join("")}</aside>` : "";
}
function miniAgents(list) {
  return layout.groups.map((kind) => {
    const items = list.filter((a) => (a.agent || "claude") === kind);
    if (!items.length) return "";
    return `<div class="mini-group">${providerLogo(kind)}${items.map((a) => {
      const same = items.filter((other) => other.name === a.name);
      const unseen = a.state === "done" && completionSeen.get(a.id) !== a.state_since;
      return `<button type="button" class="mini-session" data-open-agent="${esc(a.id)}" data-id="${esc(a.id)}" data-state="${a.state}"
        data-name="${esc(a.name)}" data-seed="${esc(same.length > 1 ? a.id : a.name)}" data-unseen="${unseen}"
        aria-label="Show ${esc(a.name)}, ${PROVIDERS[kind]}, ${esc(a.state)}" title="${esc(a.name)} · ${esc(a.state)}">
        <canvas width="12" height="12" aria-hidden="true"></canvas>${unseen ? `<span class="mini-completion" aria-label="Unseen completion">✓</span>` : ""}</button>`;
    }).join("")}</div>`;
  }).join("");
}
function renderAgents(sec) {
  if (drag) { pendingRender = true; return; }
  if ($(".agents-main", sec)) agentScroll.agents = $(".agents-main", sec).scrollTop;
  if ($(".usage-details", sec)) agentScroll.usage = $(".usage-details", sec).scrollTop;
  if ($(".agents-mini", sec)) agentScroll.mini = $(".agents-mini", sec).scrollTop;
  const focused = sec.contains(document.activeElement) ? document.activeElement : null;
  const focusToggle = focused?.matches("[data-agent-view-toggle]");
  const focusMini = focused?.dataset.openAgent;
  const list = [...S.agents];
  const w = list.filter((a) => a.state === "working").length, n = list.filter((a) => a.state === "waiting").length;
  const header = `<h2 class="agents-header"><button type="button" data-agent-view-toggle aria-label="${agentView === "agents" ? "Show detailed usage" : "Show agents"}">${agentView === "agents" ? "Agents" : "Usage"}</button>
    <small class="session-count" title="${w} working · ${n} waiting">${list.length} ${list.length === 1 ? "session" : "sessions"}</small></h2>`;
  let html = "";
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
        aria-label="Move ${esc(label)} group" title="Drag to reorder, or use the up and down arrow keys"><span class="drag-grip" aria-hidden="true"></span>${providerLogo(kind)}${esc(label)}</button></h3>
        <div class="grid">${items.map((a) => tileHtml(a, items)).join("")}</div></div>`;
    }).join("");
  }
  sec.classList.add("agents-page");
  sec.dataset.dock = layout.dock;
  sec.dataset.mode = agentView;
  sec.innerHTML = header + (agentView === "usage" ?
    `<aside class="agents-mini" aria-label="Agent sessions">${miniAgents(list)}</aside><div class="usage-details">${renderUsage(list, true)}</div>` :
    `<div class="agents-main">${html}</div>${renderUsage(list)}`);
  for (const [selector, key] of [[".agents-main", "agents"], [".usage-details", "usage"], [".agents-mini", "mini"]]) {
    const pane = $(selector, sec); if (pane) pane.scrollTop = agentScroll[key];
  }
  if (focusToggle) $("[data-agent-view-toggle]", sec)?.focus({ preventScroll: true });
  if (focusMini) [...sec.querySelectorAll("[data-open-agent]")].find((el) => el.dataset.openAgent === focusMini)?.focus({ preventScroll: true });
  animateTiles();
}
function animateTiles() {
  const now = performance.now();
  document.querySelectorAll(".tile, .mini-session").forEach((t) => {
    let motion = motions.get(t.dataset.id);
    if (!motion || motion.state !== t.dataset.state) {
      motion = creatureMotion(t.dataset.state, now);
      motions.set(t.dataset.id, motion);
    }
    const pose = stepCreatureMotion(motion, now);
    const thinking = $(".thinking", t); if (thinking) thinking.hidden = !pose.thinking;
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
  if (!handle || e.button !== 0 || drag || e.target.closest("[data-show-usage]")) return;
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
    <button type="button" class="reset-layout" data-reset-layout>Reset agent layout</button>
    <button type="button" class="reset-layout" data-forget-display>Disconnect this display</button>` +
    (S.cfg.desktop ? `<h3 class="settings-heading">Desktop setup</h3>
    <button type="button" class="setting-row" data-desktop="open-config">Open configuration folder</button>
    <button type="button" class="setting-row" data-desktop="connect-agents">Connect installed agents</button>
    <button type="button" class="setting-row" data-desktop="spotify-connect">Connect Spotify</button>
    <button type="button" class="setting-row" id="startup-toggle" data-desktop="startup-on">Start at login: Off</button>
    <p class="settings-note" id="desktop-status" role="status">Configuration changes take effect after restarting piDeck.</p>` : "");
  if (S.cfg.desktop) refreshDesktop();
}
async function refreshDesktop() {
  try {
    const response = await fetch("/api/desktop", authInit());
    if (!response.ok) return;
    const state = await response.json(), button = $("#startup-toggle");
    if (button) { button.dataset.desktop = state.startup ? "startup-off" : "startup-on"; button.textContent = "Start at login: " + (state.startup ? "On" : "Off"); }
  } catch {}
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
  if (!el.hidden) { if (alertQueue.length >= 24) alertQueue.shift(); alertQueue.push(a); return; }
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
    if (S.seenSlack.size > 300) S.seenSlack = new Set(value.map((m) => m.id));
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
// Bootstrap tokens use fragments, then become short-lived HttpOnly cookies.
// Remove the previous version's persistent credential without reusing it.
try { localStorage.removeItem("pideck_token"); } catch {}
let sessionProof = "";
try { sessionProof = sessionStorage.getItem("pideck_session_proof") || ""; } catch {}
const authInit = (init = {}) => ({ ...init, credentials: "same-origin", headers: { ...(init.headers || {}), ...(sessionProof ? { "X-piDeck-Session": sessionProof } : {}) } });
let activeSocket = null, reconnectTimer = null;
function showLogin(message = "") {
  $("#login").hidden = false;
  $("#login-error").textContent = message;
  $("#display-token").focus();
}
async function signIn(token) {
  if (location.protocol !== "https:" && !["localhost", "127.0.0.1", "[::1]"].includes(location.hostname)) {
    showLogin("Use the server's HTTPS address before entering a token."); return false;
  }
  try {
    const response = await fetch("/api/session", { method: "POST", credentials: "same-origin", headers: { Authorization: "Bearer " + token } });
    if (response.ok) {
      sessionProof = (await response.json()).proof;
      try { sessionStorage.setItem("pideck_session_proof", sessionProof); } catch {}
      $("#login").hidden = true; return true;
    }
  } catch {}
  showLogin("Could not connect. Check the display token and server address."); return false;
}
$("#login").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("#display-token"), token = input.value;
  input.value = "";
  if (await signIn(token)) connect();
});

function connect() {
  clearTimeout(reconnectTimer);
  if (activeSocket && activeSocket.readyState < 2) return;
  const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws",
    ["pideck-v1", ...(sessionProof ? ["pideck-proof." + sessionProof] : [])]);
  activeSocket = ws;
  ws.onopen = () => { $("#login").hidden = true; $("#conn").classList.remove("down"); connect.delay = 500; };
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type === "snapshot") {
      for (const k of Object.keys(m.state)) onState(k, m.state[k]);
      S.initialized = true;
    } else if (m.type === "state") onState(m.key, m.value);
    else if (m.type === "alert") showAlert(m.alert);
  };
  ws.onclose = async () => {
    activeSocket = null;
    $("#conn").classList.add("down");
    try {
      const response = await fetch("/api/session", authInit());
      if ([400, 401, 403, 426].includes(response.status)) { showLogin(); return; }
    } catch {}
    reconnectTimer = setTimeout(connect, connect.delay = Math.min((connect.delay || 500) * 1.6, 8000));
  };
}

// ---------- boot ----------
document.addEventListener("click", async (e) => {
  const desktopAction = e.target.closest("[data-desktop]")?.dataset.desktop;
  if (desktopAction) {
    const status = $("#desktop-status");
    status.textContent = "Working…";
    try {
      const response = await fetch("/api/desktop/" + desktopAction, authInit({ method: "POST" }));
      const result = await response.json();
      status.textContent = response.ok ? desktopAction === "spotify-connect" ? result.spotify : "Done" : result.detail || "Could not complete setup";
      await refreshDesktop();
    } catch { status.textContent = "Could not reach piDeck"; }
    return;
  }
  if (e.target.closest("[data-forget-display]")) {
    await fetch("/api/session", authInit({ method: "DELETE" }));
    sessionProof = "";
    try { sessionStorage.removeItem("pideck_session_proof"); } catch {}
    clearTimeout(reconnectTimer); activeSocket?.close();
    S.spotify = null; S.slack = []; S.agents = []; S.agent_usage = {}; renderAll(); showLogin(); return;
  }
  if (performance.now() < suppressClickUntil) { e.preventDefault(); return; }
  if (e.target.closest("[data-agent-view-toggle]")) {
    agentView = agentView === "agents" ? "usage" : "agents";
    selectedUsage = null;
    renderAgents($("#v-agents"));
    $("[data-agent-view-toggle]")?.focus({ preventScroll: true });
    return;
  }
  const usage = e.target.closest("[data-show-usage]");
  if (usage) {
    agentView = "usage"; selectedUsage = usage.dataset.showUsage;
    renderAgents($("#v-agents"));
    $("[data-agent-view-toggle]")?.focus({ preventScroll: true });
    return;
  }
  const mini = e.target.closest("[data-open-agent]");
  if (mini) {
    const id = mini.dataset.openAgent, agent = S.agents.find((a) => a.id === id);
    if (agent?.state === "done") { completionSeen.set(id, agent.state_since); saveCompletionSeen(); }
    agentView = "agents";
    renderAgents($("#v-agents"));
    [...document.querySelectorAll(".tile")].find((el) => el.dataset.id === id)?.scrollIntoView({ block: "nearest", inline: "nearest" });
    $("[data-agent-view-toggle]")?.focus({ preventScroll: true });
    return;
  }
  const dock = e.target.closest("button[data-dock]")?.dataset.dock;
  if (dock) { layout.dock = dock; saveLayout(); renderAll(); return; }
  if (e.target.closest("[data-reset-layout]")) {
    layout.groups = [...GROUPS]; layout.dock = "right"; saveLayout(); renderAll(); return;
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
  if (e.target.closest(".usage-grab") && e.key.startsWith("Arrow")) {
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
  const bootstrap = new URLSearchParams(location.hash.slice(1)).get("token");
  if (bootstrap || new URLSearchParams(location.search).has("token")) history.replaceState(null, "", location.pathname);
  if (bootstrap) await signIn(bootstrap);
  try { Object.assign(S.cfg, await (await fetch("/api/config")).json()); } catch {}
  applyRotate();
  buildShell();
  renderAll();
  connect();
  setInterval(() => { frame = Math.floor(performance.now() / 450); animateTiles(); }, 100);
  setInterval(async () => {
    if (!$("#login").hidden) return;
    try {
      const response = await fetch("/api/session", authInit({ method: "POST" }));
      if (!response.ok) showLogin("Please reconnect this display.");
      else {
        sessionProof = (await response.json()).proof;
        try { sessionStorage.setItem("pideck_session_proof", sessionProof); } catch {}
      }
    } catch {}
  }, 3 * 3600 * 1000);
  setInterval(() => { tickProgress(); tickClock(); }, 500);
  setInterval(() => document.querySelectorAll("[data-ago]").forEach((el) => (el.textContent = ago(+el.dataset.ago))), 15000);
  setInterval(() => { const sec = $("#v-agents"); if (sec) renderAgents(sec); }, 15000);
  S.initialized = false;
})();
