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

(async () => {
  // 1. Panel opens with the context parked (bubble mode parks it when idle).
  T.ctx.suspend();
  const p = T.setLive(true);
  await run(300); await p;
  check("parked context: mic hears within 300 ms, no mute/unmute", T.live && T.heardAt > 0 && now - T.heardAt < 50, `heardAt=${T.heardAt} now=${now}`);

  // 2. Back from the background: Android gave the mic to the bubble, the
  // page's track came back dead, the panel still says "listening".
  srcStream.getAudioTracks()[0].readyState = "ended";
  const g0 = gum, t0 = now, h0 = T.heardAt;
  while (T.heardAt === h0 && now - t0 < 6000) await run(50);
  const back = now - t0;
  await run(500);
  check("dead track: the page rebuilds the mic by itself", gum > g0 && now - T.heardAt < 50, `gum ${g0}->${gum}`);
  check("...and says so (mic healed)", sent.some(m => String(m).includes("mic healed (track ended")));
  check(`...hearing again within 3 s (${back} ms)`, back < 3000);

  // 3. Context parked again and resume() hangs waiting for a gesture: the
  // mic start must not hang with it (the old await never returned).
  T.setLive(false); await run(100);
  T.ctx.suspend(); resumeMode = "hang";
  let done = false;
  T.setLive(true).then(() => { done = true; });
  await run(800);
  check("resume() hangs: switching the mic on still finishes", done && T.live);
  // ...and once the context can run again, frames come without a toggle.
  resumeMode = "ok";
  await run(3000);
  check("context wakes later: the watchdog gets frames flowing", now - T.heardAt < 50, `heardAt=${T.heardAt} now=${now}`);

  // 4. Hidden page (panel behind other apps): the watchdog leaves it alone.
  srcStream.getAudioTracks()[0].readyState = "ended";
  doc.hidden = true;
  const g1 = gum;
  await run(3000);
  check("hidden page: no mic rebuild (the bubble owns the mic)", gum === g1);
  doc.hidden = false;
  await run(3000);
  check("visible again: mic rebuilt without a toggle", gum > g1 && now - T.heardAt < 50);

  console.log(fails ? `${fails} FAILED` : "ALL PASS");
  process.exit(fails ? 1 : 0);
})();
