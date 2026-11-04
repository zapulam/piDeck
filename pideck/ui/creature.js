// Pixel fruit for agent tiles. 12x12 grid drawn to a canvas and scaled with
// image-rendering: pixelated. Each agent gets a fruit picked from its name (stable per name);
// the state shows up in the face and decorations rather than the fruit's own colour.
// Grid legend: R/r/d = body / highlight / shadow, G/g leaf, B stem, S seed.
const LEAF = { G: "#4cc26a", g: "#2f8f4a", B: "#8b5a2b", S: "#ffe38a" };
const FRUIT = {
  raspberry: { c: ["#d7264c", "#ff7b9c", "#8f1232"], eye: 6, rows: [
    "............", "....G..G....", "...GGGGGG...", "..RrRRrRRR..", ".RRRrRRrRRR.", ".RrRRrRRrRR.",
    ".RRRrRRrRRR.", ".RrRRrRRrRR.", ".RRRrRRrRRR.", "..RRrRRrRR.."] },
  strawberry: { c: ["#f0384f", "#ff8a98", "#a01b2e"], eye: 5, rows: [
    "............", "....G..G....", "..GGGGGGGG..", ".RRRRRRRRRR.", ".RRSRRRRSRR.", ".RRRRSRRRRR.",
    ".RSRRRRRRSR.", "..RRRSRRRR..", "...RSRRRR...", "....RRRR...."] },
  orange: { c: ["#ff9a1f", "#ffd27a", "#c46b00"], eye: 6, rows: [
    "............", "......GG....", ".....GGG....", "..RRRRRRRR..", ".RrRRdRRRRR.", ".RRdRRRRRdR.",
    ".RRRRRRRRRR.", ".RRRRRRdRRR.", ".RRRdRRRRRR.", "..RRRRRRRR.."] },
  pear: { c: ["#b7d94a", "#e3f28d", "#7d9a24"], eye: 6, rows: [
    "............", ".....B......", ".....BGG....", "....RRRR....", "...RRRRRR...", "...RrRRRR...",
    "..RRRRRRRR..", ".RRRRRRRRRR.", ".RRRRRRRRRR.", "..RRRRRRRR.."] },
  blueberry: { c: ["#4a62d6", "#8ea2ff", "#2c3a8c"], eye: 6, rows: [
    "............", "............", ".....dd.....", "..RRRddRRR..", ".RrRRRRRRRR.", ".RRRRRRRRRR.",
    ".RRRRRRRRRR.", ".RRRRRRRRRR.", ".RRRRRRRRRR.", "..RRRRRRRR.."] },
  lemon: { c: ["#ffe14d", "#fff3a0", "#c9a800"], eye: 6, rows: [
    "............", "......GG....", ".....G......", "..RRRRRRRR..", ".RRrRRRRRRR.", ".RRRRRRRRRR.",
    "RRRRRRRRRRRR", "RRRRRRRRRRRR", ".RRRRRRRRRR.", "..RRRRRRRR.."] },
  apple: { c: ["#e0343a", "#ff8c8c", "#8e1c20"], eye: 6, rows: [
    "............", ".....B..GG..", ".....B.GG...", "..RRRR.RRR..", ".RrRRRRRRRR.", ".RRRRRRRRRR.",
    ".RRRRRRRRRR.", ".RRRRRRRRRR.", ".RRRRRRRRRR.", "..RRRRRRRR.."] },
  grape: { c: ["#8e5bd0", "#c9a5ff", "#583291"], eye: 6, rows: [
    "............", ".....B......", ".....BG.....", "..RrRRrRRR..", ".RrRdRrRdRR.", ".RRRdRRRdRR.",
    ".RrRdRrRdRR.", ".RRRdRRRdRR.", "..RrRdRrRR..", "....RRRR...."] },
};
const CAST = Object.keys(FRUIT);
// Agents whose name is a fruit, or the project itself, always get their own fruit.
const PINNED = { pideck: "raspberry" };

function hashName(s) {
  let h = 0;
  for (const c of s) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return h;
}

function fruitFor(name = "") {
  const n = name.trim().toLowerCase();
  if (Object.hasOwn(PINNED, n)) return PINNED[n];
  if (Object.hasOwn(FRUIT, n)) return n;
  return CAST[hashName(n) % CAST.length];
}

const hex = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
const toHex = (a) => "#" + a.map((v) => Math.round(v).toString(16).padStart(2, "0")).join("");
const mix = (a, b, t) => { const A = hex(a), B = hex(b); return toHex(A.map((v, i) => v + (B[i] - v) * t)); };
// error: a dull, washed-out version of the fruit's own colour
const dull = (h) => { const [r, g, b] = hex(h); const y = .3 * r + .59 * g + .11 * b; return mix(h, toHex([y, y, y]), .75); };

const ACCESSORIES = ["cowboy", "graduation", "baseball", "sunglasses"];
const randomBetween = (min, max) => min + Math.random() * (max - min);

// Keep these clocks per session, outside the DOM, so unrelated updates don't reset them.
function creatureMotion(state, now) {
  return { state, blinkAt: now + randomBetween(2000, 6500), blinkUntil: 0,
    lookAt: now + randomBetween(2500, 7000), lookUntil: 0, look: 0,
    thinkAt: now + randomBetween(2500, 8000), thinkUntil: 0 };
}
function stepCreatureMotion(m, now) {
  if (m.state === "idle") {
    if (now >= m.blinkAt) {
      m.blinkUntil = now + 200;
      m.blinkAt = m.blinkUntil + randomBetween(2500, 6500);
    }
    if (now >= m.lookAt) {
      m.look = Math.random() < .5 ? -1 : 1;
      m.lookUntil = now + randomBetween(700, 1400);
      m.lookAt = m.lookUntil + randomBetween(2500, 7000);
    }
  }
  if (m.state === "working" && now >= m.thinkAt) {
    m.thinkUntil = now + randomBetween(1800, 3000);
    m.thinkAt = m.thinkUntil + randomBetween(4000, 10000);
  }
  return { blink: now < m.blinkUntil, look: now < m.lookUntil ? m.look : 0,
    thinking: now < m.thinkUntil };
}

// frame: integer tick (animations key off it). Draws one fruit into `canvas`.
// `seed` picks the fruit when several sessions share a name; otherwise the name does.
function drawCreature(canvas, { state, name = "", seed, frame = 0, blink = false, look = 0, accessory }) {
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, 12, 12);
  const f = FRUIT[fruitFor(seed || name)];
  let [R, r, d] = f.c;
  if (state === "done") [R, r, d] = [R, r, d].map((c) => mix(c, "#101820", .25));   // asleep: dimmer
  if (state === "error") [R, r, d] = [R, r, d].map(dull);                          // error: dull colours
  const px = (x, y, c) => { ctx.fillStyle = c; ctx.fillRect(x, y, 1, 1); };
  const map = { R, r, d, ...LEAF };
  f.rows.forEach((row, y) => [...row].forEach((ch, x) => {
    if (ch === ".") return;
    px(x, y, map[ch] || R);
  }));

  // eyes: 2x2 dark with a single white pixel top-left
  const e = f.eye, P = "#10131a";
  const open = (dx = 0) => [[3, 4], [7, 8]].forEach(([a, b]) => {
    [a, b].forEach((x) => { px(x + dx, e, P); px(x + dx, e + 1, P); });
    px(a + dx, e, "#ffffff");
  });
  if (state === "done" || blink) [3, 4, 7, 8].forEach((x) => px(x + look, e + 1, P));   // closed
  else if (state === "idle") open(look);
  else open();

  // state decorations
  if (state === "done" && frame % 4 < 3) [[9, 0], [10, 0], [10, 1]].forEach(([x, y]) => px(x, y, "#cfe9ff"));   // z
  if (state === "waiting" && frame % 2 === 0) { ctx.fillStyle = "#fff"; ctx.fillRect(10, 0, 1, 2); ctx.fillRect(10, 3, 1, 1); }   // "!"
  if (state === "error") { px(10, e - 1, "#7fd8ff"); px(10, e, "#7fd8ff"); }   // sweat drop

  // Celebration accessories fit the same pixel grid, including the taller fruit.
  const rect = (x, y, w, h, color) => { ctx.fillStyle = color; ctx.fillRect(x, y, w, h); };
  if (accessory === "cowboy") {
    rect(3, 0, 6, 3, "#c68a45"); rect(3, 2, 6, 1, "#774626");
    rect(1, 3, 10, 1, "#e7b76d"); px(0, 2, "#e7b76d"); px(11, 2, "#e7b76d");
  } else if (accessory === "graduation") {
    rect(3, 1, 6, 3, "#283349"); rect(1, 1, 10, 1, "#10131a");
    rect(3, 0, 6, 1, "#10131a"); rect(10, 2, 1, 3, "#ffd267"); px(9, 4, "#ffd267");
  } else if (accessory === "baseball") {
    rect(3, 0, 5, 1, "#8abaff"); rect(2, 1, 7, 2, "#4388ef");
    rect(2, 3, 10, 1, "#2462b7"); px(6, 1, "#ffffff");
  } else if (accessory === "sunglasses") {
    rect(2, e, 8, 1, P); rect(2, e, 3, 2, P); rect(7, e, 3, 2, P);
    px(3, e, "#8abaff"); px(8, e, "#8abaff");
  }
}
