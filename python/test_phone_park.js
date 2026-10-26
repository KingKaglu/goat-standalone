// Voice-player parking test for phone.html (run: node test_phone_park.js).
// His bug 2026-10-26: "the phone speech is really, really bad". The page
// parks its AudioContext 1.5 s after the last sound (so Android stops calling
// it "music"), but Gemini Live streams a reply with gaps between sentences:
// a long gap parked the context mid-reply and the next sentence arrived while
// it was suspended (log: 10 chunks "ctx=suspended"), so it came late and
// stuttered. Loads the page's real <script> with a fake Web Audio whose clock
// only runs while the context runs.
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
let now = 0, resumeLagUntil = 0, played = [];
const timers = [], intervals = [], sent = [], live = [];
function fakeTimeout(fn, ms) { timers.push({fn, at: now + (ms || 0)}); return timers.length; }
function fakeClear(id) { if (id && timers[id - 1]) timers[id - 1].fn = null; }
function runTimers() { for (const t of timers) if (t.fn && t.at <= now) { const f = t.fn; t.fn = null; f(); } }

class FakeCtx {
  constructor() { this.sampleRate = 16000; this.currentTime = 0; this.state = "running"; this.destination = {}; this.audioWorklet = {addModule: async () => {}}; }
  resume() {
    // Android can sit on resume() for a while (no gesture yet); the old page
    // asked once and hoped.
    if (now < resumeLagUntil) return new Promise(() => {});
    this.state = "running"; return Promise.resolve();
  }
  suspend() { this.state = "suspended"; return Promise.resolve(); }
  createGain() { return { gain: { value: 1, setTargetAtTime() {} }, connect(x) { return x; }, disconnect() {} }; }
  createAnalyser() { return { fftSize: 512, connect(x) { return x; }, getFloatTimeDomainData(b) { b.fill(0); } }; }
  createBuffer(_c, n) { const d = new Float32Array(n); return { duration: n / 16000, getChannelData: () => d }; }
  createBufferSource() {
    const s = { connect() {}, stop() { s.dead = true; }, onended: null,
      start(t, off) { s.end = t + s.buffer.duration - (off || 0); s.wall = now; s.ctxState = ctx().state; live.push(s); } };
    return s;
  }
  createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
}
function AudioWorkletNode() { this.port = {}; this.connect = x => x; this.disconnect = () => {}; }
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
  navigator: { mediaDevices: { getUserMedia: async () => ({ getAudioTracks: () => [], getTracks: () => [] }) } },
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
  ensureCtx, get ctx() { return ctx; },
  setWs() { if (!ws || !ws.onmessage) connect(); ws.readyState = 1; ws.send = m => __sent(m); },
  msg(d) { ws.onmessage({data: d}); },
};`, sandbox);
sandbox.__sent = m => sent.push(m);
const T = sandbox.__t;
const ctx = () => T.ctx;
T.ensureCtx(); T.setWs();

const flush = () => new Promise(r => setImmediate(r));
// The fake engine: the context clock runs only while "running"; a source ends
// (onended) when the clock passes its end.
async function run(ms) {
  for (let t = 0; t < ms; t += 10) {
    now += 10;
    if (ctx().state === "running") ctx().currentTime += 0.01;
    for (const s of live.slice()) if (!s.dead && ctx().currentTime >= s.end) {
      s.dead = true; live.splice(live.indexOf(s), 1); if (s.onended) s.onended();
    }
    runTimers();
    for (const iv of intervals) if (iv.at <= now) { iv.at += iv.ms; iv.f(); }
    await flush();
  }
}
const chunk = sec => new Int16Array(Math.round(16000 * sec)).buffer;
const json = o => JSON.stringify(o);
let fails = 0;
const check = (name, ok, info = "") => { console.log((ok ? "PASS " : "FAIL ") + name + (ok ? "" : "  " + info)); if (!ok) fails++; };

(async () => {
  // 1. A Live reply: a sentence, a 3 s gap while Gemini streams the next, then more.
  T.msg(json({type: "reply"}));
  T.msg(chunk(0.5));
  await run(600);                                     // sentence played out
  await run(3000);                                    // the gap (old page parked at 1.5 s)
  check("3 s gap mid-reply: the voice player stays awake", ctx().state === "running", ctx().state);
  T.msg(chunk(0.5));
  const s2 = live[live.length - 1];
  check("...next sentence starts at once, not behind a frozen clock", s2 && s2.ctxState === "running");
  await run(600);

  // 2. turn_done: the reply is over, so park as before (Android music flag).
  T.msg(json({type: "turn_done"}));
  await run(1700);
  check("turn_done: parked 1.5 s later (no 'music active' between replies)", ctx().state === "suspended", ctx().state);

  // 3. A reply that never sends turn_done: held 8 s after its last audio, then parked.
  T.msg(json({type: "reply"}));
  await run(300);
  check("reply message wakes the player before the audio arrives", ctx().state === "running", ctx().state);
  T.msg(chunk(0.3));
  await run(5000);
  check("no turn_done: still awake 5 s after the last sound", ctx().state === "running", ctx().state);
  await run(6000);
  check("no turn_done: parked within ~10 s of the last sound", ctx().state === "suspended", ctx().state);

  // 4. Parked, and resume() hangs for 1.2 s: the page keeps asking and the
  // reply plays as soon as Android lets it.
  resumeLagUntil = now + 1200;
  T.msg(chunk(0.5));
  await run(400);
  check("resume lagging: still suspended at first (as on his phone)", ctx().state === "suspended");
  await run(1500);
  check("...page keeps retrying and the voice runs once allowed", ctx().state === "running", ctx().state);
  check("...and logs it (ctx resumed after N retries)", sent.some(m => String(m).includes("ctx resumed after")));

  // 5. cancel ends the reply hold too.
  await run(1000);
  T.msg(json({type: "reply"})); T.msg(chunk(0.2));
  await run(300);
  T.msg(json({type: "cancel"}));
  await run(1700);
  check("cancel: parked 1.5 s later", ctx().state === "suspended", ctx().state);

  console.log(fails ? `${fails} FAILED` : "ALL PASS");
  process.exit(fails ? 1 : 0);
})();
