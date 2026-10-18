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
  console, Math, JSON, Float32Array, Int16Array, Uint8Array, ArrayBuffer, Date, Promise, Blob: function () {},
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
  get inSpeech() { return inSpeech; }, get echoK() { return echoK; },
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
feed(8, 0.6, 0.2);
check("him over me -> I pause at once", T.paused > 0 && T.inSpeech && T.sources === 0, `paused=${T.paused}`);
check("the pause keeps the rest of my answer", T.held === 1);
check("the laptop is told (barge), no 'cut'", sent.some(m => String(m).includes('"barge"')) && !sent.some(m => String(m).includes('"cut"')));
// 4. The laptop says it was echo -> I carry on.
feed(45, 0.0, 0.0);                      // he stops; utterance ends (800 ms quiet)
T.msg({type: "idle"});
check("laptop says 'not him' -> I carry on", !T.paused && T.sources === 1);
// 5. Real barge: pause, then "you" -> the rest of that answer is gone.
feed(150, 0.18, 0.2);
feed(8, 0.6, 0.2);
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
feed(8, 0.6, 0.2); feed(45, 0.0, 0.0);
check("third barge pauses", T.paused > 0);
feed(320, 0.0, 0.0);
check("no verdict for 6 s -> I carry on", !T.paused && T.sources >= 1);
// 8. A short click/tap (2 frames) never pauses.
feed(150, 0.18, 0.2);
feed(2, 0.9, 0.2); feed(20, 0.18, 0.2);
check("a tap noise never pauses", !T.paused);
// 9. "Stop, stop" dips between syllables: still caught (old rule missed it).
for (let k = 0; k < 3 && !T.paused; k++) { feed(3, 0.6, 0.2); feed(1, 0.1, 0.2); }
check("a choppy 'stop, stop' pauses me", T.paused > 0);
feed(10, 0.6, 0.0); feed(45, 0.0, 0.0); T.msg({type: "you", text: "stop"}); T.msg({type: "reply"});
check("...and 'stop' heard -> stopped", !T.paused && T.sources === 0);
// 10. A cough over me: too short to be words -> I carry on, no laptop trip.
T.bin(new Int16Array(16000 * 3).buffer); feed(150, 0.18, 0.2);
const before = sent.length;
feed(7, 0.6, 0.2); feed(45, 0.0, 0.0);
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
console.log(fails ? `${fails} FAILED` : "ALL PASS");
process.exit(fails ? 1 : 0);
