// Original pixel critters for agent tiles. 12x12 grid drawn to a canvas and scaled with
// image-rendering: pixelated. Body colour = state, antenna bulbs = a per-agent colour.
const SPRITE = {
  W: 12,
  body: [
    "............",
    "............",
    "..AA....AA..",   // antenna bulbs
    "...a....a...",   // stalks
    "..BBBBBBBB..",
    ".BBBBBBBBBB.",
    ".BBBBBBBBBB.",
    ".BBBBBBBBBB.",
    ".BBBBBBBBBB.",
    "..BBBBBBBB..",
    "............",
    "............",
  ],
};

const STATE_COLOR = {
  working: "#ff9a3c",
  waiting: "#ff4d8d",
  done:    "#5fd3b0",
  idle:    "#7d8bab",
  error:   "#ff4040",
};

function hashHue(s) {
  let h = 0;
  for (const c of s) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return h % 360;
}

function shade(hex, amt) {
  const n = parseInt(hex.slice(1), 16);
  const f = (v) => Math.max(0, Math.min(255, Math.round(v + amt)));
  return "#" + [(n >> 16) & 255, (n >> 8) & 255, n & 255].map(f).map((v) => v.toString(16).padStart(2, "0")).join("");
}

// frame: integer tick (animations key off it). Draws one critter into `canvas`.
// kind: "claude" (antennae) or "codex" (round ears + forehead gem)
function drawCreature(canvas, { state, name, frame, kind = "claude" }) {
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, 12, 12);
  const body = STATE_COLOR[state] || STATE_COLOR.idle;
  const dim = state === "done" || state === "idle";
  const bodyC = dim ? shade(body, -45) : body;
  const dark = shade(bodyC, -90);
  const bulb = `hsl(${hashHue(name)} 85% 65%)`;
  const bob = state === "working" && frame % 2 ? 1 : 0;   // working: bounce
  const px = (x, y, c) => { ctx.fillStyle = c; ctx.fillRect(x, y + bob, 1, 1); };

  const rows = SPRITE.body.slice();
  if (kind === "codex") { rows[2] = ".BB......BB."; rows[3] = ".BBB....BBB."; }
  rows.forEach((row, y) => {
    [...row].forEach((ch, x) => {
      if (ch === "B") px(x, y, bodyC);
      else if (ch === "A") px(x, y, bulb);
      else if (ch === "a") px(x, y, dark);
    });
  });

  if (kind === "codex") { px(5, 4, bulb); px(6, 4, bulb); }

  // eyes
  const blink = state === "idle" && frame % 8 === 7;
  const asleep = state === "done" || blink;
  if (state === "error") {
    [[3, 6], [4, 7], [4, 6], [3, 7], [7, 6], [8, 7], [8, 6], [7, 7]].forEach(([x, y]) => px(x, y, dark));
  } else if (asleep) {
    [3, 4, 7, 8].forEach((x) => px(x, 7, dark));
  } else {
    [[3, 6], [3, 7], [4, 6], [4, 7], [7, 6], [7, 7], [8, 6], [8, 7]].forEach(([x, y]) => px(x, y, "#10131a"));
    // little highlight
    px(3, 6, "#ffffff"); px(7, 6, "#ffffff");
  }

  // feet (alternate while working)
  const feet = state === "working" && frame % 2 ? [3, 4, 7, 8] : [2, 4, 7, 9];
  feet.forEach((x) => { ctx.fillStyle = dark; ctx.fillRect(x, 10 + bob, 1, 1); });

  // state decorations
  if (state === "done" && frame % 4 < 3) {                  // z z
    px(9, 1, "#cfe9ff"); px(10, 1, "#cfe9ff"); px(10, 0, "#cfe9ff");
  }
  if (state === "waiting") {                                // blinking "!"
    if (frame % 2 === 0) {
      ctx.fillStyle = "#fff";
      ctx.fillRect(5, 0, 2, 2); ctx.fillRect(5, 3, 2, 1);
    }
  }
  if (state === "working") {                                // sparkle
    const s = [[10, 3], [1, 4], [10, 7]][frame % 3];
    px(s[0], s[1], "#ffe9a8");
  }
}
