// Mic start-up test for phone.html (run: node test_phone_mic.js).
// His bug 2026-10-19: "I have to press mute, unmute so it starts recording,
// even though it isn't on mute." The page said "listening" while no frame
// reached the mic worklet: the audio context was parked, or Android had taken
// the mic for the bubble and the track came back dead. Loads the page's real
// <script> with a fake Web Audio where frames flow only while the context runs
// and the track is live, and checks the page starts hearing by itself.
const fs = require("fs"), path = require("path"), vm = require("vm");
const html = fs.readFileSync(path.join(__dirname, "phone.html"), "utf8");
const code = html.match(/<script>([\s\S]*?)<\/script>/)[1];

function any() {
  const f = function () { return any(); };
  return new Proxy(f, {
    get(t, k) {
      if (k === Symbol.toPrimitive) return () => 0;
      if (k === "length") return 0;
      if (!(k in t)) t[k] = any();
      return t[k];
    },
    set(t, k, v) { t[k] = v; return true; },
  });
}
let heard = 0, now = 0, resumeMode = "ok", gum = 0, tap = null, srcStream = null;
const timers = [], intervals = [], sent = [];
function fakeTimeout(fn, ms) { timers.push({fn, at: now + (ms || 0)}); return timers.length; }
function fakeClear(id) { if (id && timers[id - 1]) timers[id - 1].fn = null; }
function runTimers() { for (const t of timers) if (t.fn && t.at <= now) { const f = t.fn; t.fn = null; f(); } }

class FakeCtx {
  constructor() { this.sampleRate = 16000; this.currentTime = 0; this.state = "running"; this.destination = {}; this.audioWorklet = {addModule: async () => {}}; }
  resume() {
    if (resumeMode === "hang") return new Promise(() => {});    // waits for a gesture that never comes
    this.state = "running"; return Promise.resolve();
  }
  suspend() { this.state = "suspended"; return Promise.resolve(); }
  createGain() { return { gain: { value: 1, setTargetAtTime() {} }, connect(x) { return x; }, disconnect() {} }; }
  createAnalyser() { return { fftSize: 512, connect(x) { return x; }, getFloatTimeDomainData(b) { b.fill(0); } }; }
  createBuffer(_c, n) { const d = new Float32Array(n); return { duration: n / 16000, getChannelData: () => d }; }
  createBufferSource() { return { connect() {}, start() {}, stop() {}, onended: null }; }
  createMediaStreamSource(s) { srcStream = s; return { connect() {}, disconnect() {} }; }
}
function AudioWorkletNode() { this.port = {}; this.connect = x => x; this.disconnect = () => {}; tap = this; }
const track = () => ({ readyState: "live", muted: false, stop() { this.readyState = "ended"; } });
const doc = any();
doc.hidden = false;
const sandbox = {
  console, Math, JSON, Float32Array, Float64Array, Int16Array, Uint8Array, Uint16Array, ArrayBuffer, Date, Promise, Blob: function () {},
  btoa: b => Buffer.from(b, "binary").toString("base64"), isFinite,
  URL: { createObjectURL: () => "" }, setTimeout: (f, ms) => fakeTimeout(f, ms), clearTimeout: id => fakeClear(id),
  setInterval: (f, ms) => { intervals.push({f, ms, at: now + ms}); return intervals.length; }, clearInterval() {},
  requestAnimationFrame: () => 0, fetch: () => new Promise(() => {}),
  performance: { now: () => now },
  document: doc, location: { host: "x", hash: "", search: "" },
  navigator: { mediaDevices: { getUserMedia: async () => { gum++; const t = track(); return { getAudioTracks: () => [t], getTracks: () => [t] }; } } },
  isSecureContext: true, AudioWorkletNode,
  localStorage: { getItem: () => null, setItem() {} },
  WebSocket: function () { this.readyState = 1; this.send = m => sent.push(m); },
  AudioContext: FakeCtx, Notification: undefined, history: any(), addEventListener() {},
  matchMedia: () => ({ matches: false, addEventListener() {} }),
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(code + `
;globalThis.__t = {
  ensureCtx, setLive, get ctx() { return ctx; }, get live() { return live; },
  setWs() { if (!ws || !ws.onmessage) connect(); ws.readyState = 1; ws.send = m => __sent(m); },
};`, sandbox);
sandbox.__sent = m => sent.push(m);
const T = sandbox.__t;
Object.defineProperty(T, "heardAt", {get: () => heard});
T.ensureCtx(); T.setWs();

const flush = () => new Promise(r => setImmediate(r));
// The fake audio engine: the worklet gets 128-sample blocks only while the
// context runs and the source's track is live (what Chrome does).
async function run(ms) {
  for (let t = 0; t < ms; t += 10) {
    now += 10;
    runTimers();
    for (const iv of intervals) if (iv.at <= now) { iv.at += iv.ms; iv.f(); }
    const tr = srcStream && srcStream.getAudioTracks()[0];
    if (tap && tap.port.onmessage && T.ctx.state === "running" && tr && tr.readyState === "live")
      { heard = now; tap.port.onmessage({data: [new Float32Array(128), null]}); }
    await flush();
  }
}
let fails = 0;
const check = (name, ok, info = "") => { console.log((ok ? "PASS " : "FAIL ") + name + (ok ? "" : "  " + info)); if (!ok) fails++; };

// ---- VAD: when does an utterance end? (2026-10-25, Essen: the page's mic
// sent 30 s clips — the cap — whenever people talked near him, because "quiet"
// was measured against the room, and a crowd never gets quiet.)
vm.runInContext(`globalThis.__v = { frame, get inSpeech() { return inSpeech; }, FRAME, resetVad };`, sandbox);
const V = sandbox.__v;
let seed = 7;
const rnd = () => { seed = (seed * 16807) % 2147483647; return seed / 2147483647 - 0.5; };
function frameAt(level) {
  const f = new Float32Array(V.FRAME);
  for (let i = 0; i < f.length; i++) f[i] = rnd() * 2 * level * 1.7;
  return f;
}
// his speech: syllables at `lvl` with short dips between words
const him = (ms, lvl) => Array.from({length: ms / 20}, (_, k) => (k % 12 < 9 ? lvl : lvl * 0.25));
const steady = (ms, lvl) => Array.from({length: ms / 20}, () => lvl * (0.85 + 0.3 * Math.abs(rnd())));
function clipSeconds(levels) {
  V.resetVad();
  sent.length = 0;
  for (const l of levels) { now += 20; V.frame(frameAt(l), null); }
  const clip = sent.find(m => m instanceof ArrayBuffer || (m && m.byteLength));
  return clip ? clip.byteLength / 2 / 16000 : null;
}
(async () => {
  const quiet = steady(1200, 0.0015);
  let s = clipSeconds([...quiet, ...him(3000, 0.08), ...steady(3000, 0.0015)]);
  check(`him in a quiet room: the clip ends when he stops (${s} s)`, s !== null && s > 3 && s < 4.6);
  s = clipSeconds([...quiet, ...him(3000, 0.08), ...steady(25000, 0.02), ...steady(2000, 0.0015)]);
  check(`him, then a steady crowd: ends when he stops (${s} s)`, s !== null && s < 5);
  // other people's TALK: words with dips, like his, but from a few metres
  // away: ~18 dB under his voice at the phone he holds (distance alone)
  const others = ms => Array.from({length: ms / 20}, (_, k) => (k % 17 < 11 ? 0.010 : 0.002));
  s = clipSeconds([...quiet, ...him(3000, 0.08), ...others(25000), ...steady(2000, 0.0015)]);
  check(`him, then people talking a few metres away: ends when HE stops, not at the cap (${s} s)`, s !== null && s < 5);
  s = clipSeconds([...quiet, ...him(2500, 0.08), ...him(1200, 0.025), ...steady(2000, 0.0015)]);
  check(`his quieter last words still belong to the clip (${s} s)`, s !== null && s > 4.2);
  console.log(fails ? `${fails} FAILED` : "ALL PASS");
  process.exit(fails ? 1 : 0);
})();
