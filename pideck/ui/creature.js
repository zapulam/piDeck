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
  if (PINNED[n]) return PINNED[n];
  if (FRUIT[n]) return n;
  return CAST[hashName(n) % CAST.length];
}

const hex = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
const toHex = (a) => "#" + a.map((v) => Math.round(v).toString(16).padStart(2, "0")).join("");
const mix = (a, b, t) => { const A = hex(a), B = hex(b); return toHex(A.map((v, i) => v + (B[i] - v) * t)); };
// error: a dull, washed-out version of the fruit's own colour
const dull = (h) => { const [r, g, b] = hex(h); const y = .3 * r + .59 * g + .11 * b; return mix(h, toHex([y, y, y]), .75); };

// frame: integer tick (animations key off it). Draws one fruit into `canvas`.
// `kind` is accepted for compatibility but the fruit comes from the agent name.
function drawCreature(canvas, { state, name = "", frame = 0 }) {
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, 12, 12);
  const f = FRUIT[fruitFor(name)];
  let [R, r, d] = f.c;
  if (state === "done") [R, r, d] = [R, r, d].map((c) => mix(c, "#101820", .25));   // asleep: dimmer
  if (state === "error") [R, r, d] = [R, r, d].map(dull);                          // error: dull colours
  const px = (x, y, c) => { ctx.fillStyle = c; ctx.fillRect(x, y, 1, 1); };
  const map = { R, r, d, ...LEAF };
  const shine = state === "working" ? (frame * 4) % 22 - 2 : null;   // diagonal highlight sweeping over the body
  f.rows.forEach((row, y) => [...row].forEach((ch, x) => {
    if (ch === ".") return;
    let c = map[ch] || R;
    if (shine !== null && "Rrd".includes(ch) && Math.abs(x + y - shine) <= 1) c = mix(c, "#ffffff", .5);
    px(x, y, c);
  }));

  // eyes: 2x2 dark with a single white pixel top-left
  const e = f.eye, P = "#10131a";
  const open = (dx = 0) => [[3, 4], [7, 8]].forEach(([a, b]) => {
    [a, b].forEach((x) => { px(x + dx, e, P); px(x + dx, e + 1, P); });
    px(a + dx, e, "#ffffff");
  });
  if (state === "done") [3, 4, 7, 8].forEach((x) => px(x, e + 1, P));   // closed
  else if (state === "idle") open([0, -1, 0, 1][frame % 4]);            // glances left and right
  else open();

  // state decorations
  if (state === "done" && frame % 4 < 3) [[9, 0], [10, 0], [10, 1]].forEach(([x, y]) => px(x, y, "#cfe9ff"));   // z
  if (state === "waiting" && frame % 2 === 0) { ctx.fillStyle = "#fff"; ctx.fillRect(10, 0, 1, 2); ctx.fillRect(10, 3, 1, 1); }   // "!"
  if (state === "error") { px(10, e - 1, "#7fd8ff"); px(10, e, "#7fd8ff"); }   // sweat drop
}
