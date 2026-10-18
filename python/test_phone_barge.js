// Barge-in / echo guard test for phone.html's mic loop (run: node test_phone_barge.js).
// Loads the page's real <script> with a stubbed DOM + Web Audio, then feeds
// frames: my own echo must never duck me; him talking over me must; the
// laptop's verdict ("idle" = echo, "you" = him) must restore or stop my voice.
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
  createBufferSource() { return { connect() {}, start() {}, stop() {}, onended: null }; }
}
const doc = any();
const sandbox = {
  console, Math, JSON, Float32Array, Int16Array, Uint8Array, ArrayBuffer, Date, Promise, Blob: function () {},
  URL: { createObjectURL: () => "" }, setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
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
  get ducked() { return ducked; }, get sources() { return sources.length; },
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
  }
}
function speak(sec) {        // one sentence of my voice arrives and starts playing
  T.play(new Int16Array(16000 * sec).buffer);
}

// 1. Loud echo of my own voice (auto-gain: mic as loud as the speaker): no duck.
speak(3);
feed(150, 0.18, 0.2);
check("my own loud echo never ducks me", !T.ducked && T.sources === 1, `echoK=${T.echoK}`);
// 2. Echo that swings (louder syllables) still no duck.
for (let k = 0; k < 20; k++) { feed(5, 0.25, 0.25); feed(5, 0.05, 0.06); }
check("swinging echo never ducks me", !T.ducked, `echoK=${T.echoK}`);
// 3. He talks over me, clearly above my echo: duck within ~200 ms.
feed(12, 0.6, 0.2);
check("him over me -> my voice ducks", T.ducked > 0 && T.inSpeech, `ducked=${T.ducked}`);
check("barge is not a cut: no 'cut' sent", !sent.some(m => String(m).includes('"cut"')));
// 4. The laptop says it was echo -> my voice comes back up.
feed(45, 0.0, 0.2);                      // he stops; utterance ends (800 ms quiet)
T.msg({type: "idle"});
check("laptop says 'not him' -> voice back up", !T.ducked && T.sources === 1);
// 5. Real barge: duck, then "you" -> my voice stops for good.
feed(12, 0.6, 0.2);
check("second barge ducks again", T.ducked > 0);
feed(45, 0.0, 0.2);
T.msg({type: "you", text: "wait, stop"});
check("laptop heard him -> my voice stopped", !T.ducked && T.sources === 0);
// 6. His answer right after must play (no stale-audio drop after a real barge).
speak(1);
check("the reply to his barge plays", T.sources === 1);
// 7. No verdict at all within 6 s -> undo the duck on our own.
feed(12, 0.6, 0.2); feed(45, 0.0, 0.2);
check("third barge ducks", T.ducked > 0);
feed(320, 0.0, 0.2);
check("no verdict for 6 s -> voice back up", !T.ducked);
// 8. A short click/tap (2 frames) never ducks.
feed(2, 0.9, 0.2); feed(20, 0.0, 0.2);
check("a tap noise never ducks", !T.ducked);
console.log(fails ? `${fails} FAILED` : "ALL PASS");
process.exit(fails ? 1 : 0);
