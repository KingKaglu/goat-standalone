// Barge-in / echo guard test for phone.html's mic loop (run: node test_phone_barge.js).
// Loads the page's real <script> with a stubbed DOM + Web Audio, then feeds
// frames: my own echo must never pause me; him talking over me must; the
// laptop's verdict ("idle" = echo, "you" = him) must resume or drop my voice.
const fs = require("fs"), path = require("path"), vm = require("vm");
const html = fs.readFileSync(path.join(__dirname, "phone.html"), "utf8");
const code = html.match(/<script>([\s\S]*?)<\/script>/)[1];

// A do-anything object: every property is callable and chainable.
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
let now = 0, playRms = 0, gainNow = 1;
const timers = [];
function fakeTimeout(fn, ms) { timers.push({fn, at: now + (ms || 0)}); return timers.length; }
function fakeClear(id) { if (id && timers[id - 1]) timers[id - 1].fn = null; }
function runTimers() { for (const t of timers) if (t.fn && t.at <= now) { const f = t.fn; t.fn = null; f(); } }
const sent = [];
class FakeCtx {
  constructor() { this.sampleRate = 16000; this.currentTime = 0; this.state = "running"; this.destination = {}; }
  resume() {}
  createGain() { return { gain: { value: 1, setTargetAtTime(v) { gainNow = v; } }, connect(x) { return x; } }; }
  createAnalyser() {
    return { fftSize: 512, connect(x) { return x; },
      getFloatTimeDomainData(b) { for (let i = 0; i < b.length; i++) b[i] = (i % 2 ? -1 : 1) * playRms; } };
  }
  createBuffer(_c, n) { const d = new Float32Array(n); return { duration: n / 16000, getChannelData: () => d }; }
  createBufferSource() { return { connect() {}, start(t, off) { this.off = off || 0; }, stop() {}, onended: null }; }
}
const doc = any();
const sandbox = {
  console, Math, JSON, Float32Array, Float64Array, Int16Array, Uint8Array, Uint16Array, ArrayBuffer, Date, Promise, Blob: function () {},
  btoa: b => Buffer.from(b, "binary").toString("base64"), isFinite,
  URL: { createObjectURL: () => "" }, setTimeout: (f, ms) => fakeTimeout(f, ms), clearTimeout: id => fakeClear(id), setInterval: () => 0, clearInterval() {},
  requestAnimationFrame: () => 0, fetch: () => new Promise(() => {}),
  performance: { now: () => now },
  document: doc, navigator: any(), location: { host: "x", hash: "", search: "" },
  localStorage: { getItem: () => null, setItem() {} },
  WebSocket: function () { this.readyState = 1; this.send = m => sent.push(m); },
  AudioContext: FakeCtx, Notification: undefined, history: any(), addEventListener() {},
  matchMedia: () => ({ matches: false, addEventListener() {} }),
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(code + `
;globalThis.__t = {
  frame, ensureCtx, play,
  get paused() { return paused; }, get sources() { return sources.length; },
  get held() { return held.length; }, barge: () => window.goatBarge(), bargeEnd: s => window.goatBargeEnd(s),
  bin(buf) { ws.onmessage({data: buf}); },
  get inSpeech() { return inSpeech; }, get echoK() { return echoK; }, get aec() { return aec; }, get strict() { return aecStrict; },
  msg(m) { ws.onmessage({data: JSON.stringify(m)}); },
  setWs() { if (!ws || !ws.onmessage) connect(); ws.readyState = 1; ws.send = m => __sent(m); },
};`, sandbox);
sandbox.__sent = m => sent.push(m);
const T = sandbox.__t;
T.ensureCtx(); T.setWs();

let fails = 0;
const check = (name, ok, info = "") => { console.log((ok ? "PASS " : "FAIL ") + name + (ok ? "" : "  " + info)); if (!ok) fails++; };
const FRAME = 320;
function feed(n, micRms, plRms) {
  for (let i = 0; i < n; i++) {
    now += 20; playRms = plRms;
    const f = new Float32Array(FRAME);
    for (let j = 0; j < FRAME; j++) f[j] = (j % 2 ? -1 : 1) * micRms;
    T.frame(f);
    runTimers();
  }
}
function speak(sec) {        // one sentence of my voice arrives and starts playing
  T.play(new Int16Array(16000 * sec).buffer);
}

// 1. Loud echo of my own voice (auto-gain: mic as loud as the speaker): no pause.
speak(3);
feed(150, 0.18, 0.2);
check("my own loud echo never pauses me", !T.paused && T.sources === 1, `echoK=${T.echoK}`);
// 2. Echo that swings (louder syllables) still no pause.
for (let k = 0; k < 20; k++) { feed(5, 0.25, 0.25); feed(5, 0.05, 0.06); }
check("swinging echo never pauses me", !T.paused, `echoK=${T.echoK}`);
// 3. He talks over me, clearly above my echo: I stop within ~200 ms.
feed(8, 0.8, 0.2);
check("him over me -> I pause at once", T.paused > 0 && T.inSpeech && T.sources === 0, `paused=${T.paused}`);
check("the pause keeps the rest of my answer", T.held === 1);
check("the laptop is told (barge), no 'cut'", sent.some(m => String(m).includes('"barge"')) && !sent.some(m => String(m).includes('"cut"')));
// 4. The laptop says it was echo -> I carry on.
feed(45, 0.0, 0.0);                      // he stops; utterance ends (800 ms quiet)
T.msg({type: "idle"});
check("laptop says 'not him' -> I carry on", !T.paused && T.sources === 1);
// 5. Real barge: pause, then "you" -> the rest of that answer is gone.
feed(150, 0.18, 0.2);
feed(8, 0.8, 0.2);
check("second barge pauses again", T.paused > 0);
feed(45, 0.0, 0.0);
T.msg({type: "you", text: "wait, stop"});
check("laptop heard him -> I stop for good", !T.paused && T.sources === 0 && T.held === 0);
T.bin(new Int16Array(16000).buffer);
check("old answer still on the wire is dropped", T.sources === 0);
// 6. My reply to him plays.
T.msg({type: "reply"});
T.bin(new Int16Array(16000).buffer);
check("the reply to his barge plays", T.sources === 1);
// 7. No verdict at all within 6 s -> I carry on by myself.
feed(150, 0.18, 0.2);
feed(8, 0.8, 0.2); feed(45, 0.0, 0.0);
check("third barge pauses", T.paused > 0);
feed(320, 0.0, 0.0);
check("no verdict for 6 s -> I carry on", !T.paused && T.sources >= 1);
// 8. A short click/tap (2 frames) never pauses.
feed(150, 0.18, 0.2);
feed(2, 0.9, 0.2); feed(20, 0.18, 0.2);
check("a tap noise never pauses", !T.paused);
// 9. "Stop, stop" dips between syllables: still caught (old rule missed it).
for (let k = 0; k < 3 && !T.paused; k++) { feed(3, 0.8, 0.2); feed(1, 0.1, 0.2); }
check("a choppy 'stop, stop' pauses me", T.paused > 0);
feed(10, 0.6, 0.0); feed(45, 0.0, 0.0); T.msg({type: "you", text: "stop"}); T.msg({type: "reply"});
check("...and 'stop' heard -> stopped", !T.paused && T.sources === 0);
// 10. A cough over me: too short to be words -> I carry on, no laptop trip.
T.bin(new Int16Array(16000 * 3).buffer); feed(150, 0.18, 0.2);
const before = sent.length;
feed(7, 0.8, 0.2); feed(45, 0.0, 0.0);
check("a cough: pause, then carry on at once", !T.paused && T.sources === 1 && !sent.slice(before).some(m => m instanceof ArrayBuffer));
// 11. Bubble mode: the native mic calls goatBarge/goatBargeEnd.
T.msg({type: "cancel"}); T.bin(new Int16Array(16000 * 3).buffer);
check("bubble barge pauses me", T.barge() === true && T.paused > 0 && T.sources === 0);
T.bin(new Int16Array(8000).buffer);
check("audio arriving while paused waits", T.sources === 0 && T.held === 2);
T.bargeEnd(false);
check("bubble: nothing worth sending -> carry on, all of it", !T.paused && T.sources === 2);
T.barge(); T.bargeEnd(true); T.msg({type: "idle"});
check("bubble: laptop says echo -> carry on", !T.paused && T.sources >= 1);
T.barge(); T.bargeEnd(true); T.msg({type: "you", text: "hey"});
check("bubble: laptop heard him -> stopped", !T.paused && T.sources === 0 && T.held === 0);
check("bubble barge while silent does nothing", T.barge() === false && !T.paused);
// 11b. Paused for "him", but the laptop's streaming ear has no word of his
// (room noise holding the VAD open, 2026-10-19): "carry" resumes me at once,
// and noise does not make the echo rule stricter.
T.msg({type: "reply"}); T.bin(new Int16Array(16000 * 3).buffer);
const s0 = T.strict;
check("carry: paused for noise", T.barge() === true && T.paused > 0);
T.msg({type: "carry"});
check("carry: the laptop heard no words -> I carry on", !T.paused && T.sources >= 1);
check("carry: noise is not an echo false alarm", T.strict === s0, `strict ${s0} -> ${T.strict}`);
T.msg({type: "carry"});
check("carry while not paused does nothing", !T.paused && T.sources >= 1);
T.msg({type: "cancel"});
// 12. The echo canceller, with real speech through the page's own frame():
// my voice (Edge) through a fake phone speaker + room into the mic, him
// (Piper) cutting in. testdata/aec/*.wav
{
  const TD = path.join(__dirname, "testdata", "aec");
  const wav = p => {
    const b = fs.readFileSync(path.join(TD, p));
    let o = 12; while (b.toString("ascii", o, o + 4) !== "data") o += 8 + b.readUInt32LE(o + 4);
    const n = b.readUInt32LE(o + 4) / 2, x = new Float32Array(n);
    for (let i = 0; i < n; i++) x[i] = b.readInt16LE(o + 8 + 2 * i) / 32768;
    return x;
  };
  let sd = 7; const rnd = () => ((sd = (sd * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff);
  const gauss = () => { let u = 0; for (let i = 0; i < 6; i++) u += rnd(); return (u - 3) / Math.sqrt(0.5); };
  const SR = 16000, me = wav("me.wav"), stop = wav("him_stop.wav"), lng = wav("him_long.wav");
  const pre = SR, n = pre + me.length + SR, ref = new Float32Array(n);
  for (let i = 0; i < me.length; i++) ref[pre + i] = me[i] * 0.5;
  const L = 1440, h = new Float32Array(L); h[0] = 1;
  for (let i = 8; i < L; i++) h[i] = gauss() * 0.25 * Math.exp(-i / 240);
  const D = Math.round(0.18 * SR), echo = new Float32Array(n);
  for (let i = D; i < n; i++) { let v = 0; for (let k = 0; k < L && i - D - k >= 0; k++) v += h[k] * Math.tanh(3 * ref[i - D - k]) / 3; echo[i] = v; }
  let ee = 0; for (let i = pre; i < pre + me.length; i++) ee += echo[i] * echo[i];
  const g = 0.1 / Math.sqrt(ee / me.length), him = new Float32Array(n);
  const put = (c, t) => { const a = Math.round(t * SR); let r = 0; for (const v of c) r += v * v; r = Math.sqrt(r / c.length); for (let i = 0; i < c.length; i++) him[a + i] += c[i] * 0.06 / r; return a; };
  const s1 = put(stop, 6), s2 = put(lng, 12);
  const mic = new Float32Array(n);
  for (let i = 0; i < n; i++) mic[i] = echo[i] * g + him[i] + gauss() * 0.002;
  T.msg({type: "cancel"}); T.msg({type: "reply"});
  const pauses = []; let was = false, seen = 0, verdictAt = 0, s0 = sent.length;
  const played = new Float32Array(n), nlp = new Float32Array(n);
  for (let f = 0; f + FRAME <= n; f += FRAME) {
    now += 20;
    if (f === pre) T.play(new Int16Array(16000 * 30).buffer);   // my reply starts: plays throughout
    // while I'm paused for him my voice is silent; the room only echoes
    // what was actually played (causal), D samples later
    const quiet = T.paused > 0 || f < pre;
    const x = quiet ? new Float32Array(FRAME) : ref.slice(f, f + FRAME), m = new Float32Array(FRAME);
    played.set(x, f);
    for (let i = 0; i < FRAME; i++) {
      const j = f + i - D; let v = 0;
      for (let k = 0; k < L && j - k >= 0; k++) v += h[k] * nlp[j - k];
      m[i] = mic[f + i] - echo[f + i] * g + v * g;
      if (j + 1 + D < n) nlp[f + i] = Math.tanh(3 * played[f + i]) / 3;
    }
    let xe = 0; for (let i = 0; i < FRAME; i++) xe += x[i] ** 2;
    playRms = Math.sqrt(xe / FRAME) * 2;
    T.frame(m, x);
    runTimers();
    if (T.paused && !was) pauses.push(f);
    was = T.paused > 0;
    // the laptop answers every clip within ~0.5 s with "not him" (so the
    // first cut-in counts as a false alarm and the next must still work)
    const clips = sent.slice(s0).filter(m => m instanceof ArrayBuffer).length;
    if (clips > seen) { seen = clips; verdictAt = f + 8000; }
    if (verdictAt && f >= verdictAt) { verdictAt = 0; if (T.paused) T.msg({type: "idle"}); }
  }
  const at = p => Math.round((p - s1) / 16) + "ms";
  check("canceller locks on before he speaks", T.aec.ready && T.aec.delay > 0, `delay=${T.aec.delay} erle=${T.aec.erle}`);
  check("canceller found the speaker delay", Math.abs(T.aec.delay - D) < 400, `found ${T.aec.delay} true ${D}`);
  check("my echo alone never pauses me", !pauses.some(p => p < s1 - 1600), "pauses at " + pauses.map(p => (p / SR).toFixed(2)).join(" "));
  const p1 = pauses.find(p => p >= s1 - 1600);
  check("his 'stop' over my voice pauses me fast", p1 !== undefined && p1 - s1 < 0.4 * SR, p1 === undefined ? "missed" : at(p1));
  const p2 = pauses.find(p => p >= s2 - 1600);
  check("after a false alarm he can STILL stop me", p2 !== undefined && p2 - s2 < 0.5 * SR, p2 === undefined ? "missed" : Math.round((p2 - s2) / 16) + "ms");
  // the bubble: every sentence I play goes to the native canceller with its wall time
  const got = [];
  sandbox.GoatApp = {ref: (b64, wall) => got.push([b64.length, wall]), refCut: w => got.push(["cut", w]), state() {}};
  T.msg({type: "cancel"}); T.msg({type: "reply"}); T.play(new Int16Array(8000).buffer); T.msg({type: "cancel"});
  const r0 = got.find(x => typeof x[0] === "number");
  check("bubble gets my sentence + its wall time, then the cut", r0 && r0[0] > 1000 && Math.abs(r0[1] - Date.now()) < 5000 && got.indexOf(r0) < got.length - 1 && got[got.length - 1][0] === "cut", JSON.stringify(got));
  delete sandbox.GoatApp;
}
console.log(fails ? `${fails} FAILED` : "ALL PASS");
process.exit(fails ? 1 : 0);
