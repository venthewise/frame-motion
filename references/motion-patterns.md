# Motion Patterns

Pure functions of time for `seek(t)` scenes. Nothing here keeps state between frames.

## Basics

```js
const clamp = (x, a = 0, b = 1) => Math.max(a, Math.min(b, x));
const lerp = (a, b, p) => a + (b - a) * p;
// local progress of an event that starts at t0 and lasts dur
const local = (t, t0, dur) => clamp((t - t0) / dur);
```

## Seeded PRNG (never Math.random)

```js
function mulberry32(seed) {
  return function () {
    seed |= 0; seed = seed + 0x6D2B79F5 | 0;
    let t = Math.imul(seed ^ seed >>> 15, 1 | seed);
    t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
    return ((t ^ t >>> 14) >>> 0) / 4294967296;
  };
}
// Build random tables ONCE at load (same seed -> same layout every frame and every render):
const rnd = mulberry32(42);
const dots = Array.from({ length: 40 }, () => ({ x: rnd() * 1920, y: rnd() * 1080, d: rnd() }));
```

## Easing

```js
const easeOutCubic = p => 1 - Math.pow(1 - p, 3);
const easeInOutCubic = p => p < 0.5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2;
// exponential ease-out with rate k (bigger = snappier), normalised so it reaches exactly 1 at p = 1
const expoOut = (p, k = 8) => p <= 0 ? 0 : p >= 1 ? 1 : (1 - Math.exp(-k * p)) / (1 - Math.exp(-k));
const expoInOut = (p, k = 8) => p < 0.5 ? (1 - expoOut(1 - 2 * p, k)) / 2 : (1 + expoOut(2 * p - 1, k)) / 2;
```

## Critically damped spring (no overshoot)

```js
// step response from 0 to 1, settles without overshoot. w = stiffness (rad/s), ~12-25 for UI snaps
const springCD = (dt, w = 18) => dt <= 0 ? 0 : 1 - Math.exp(-w * dt) * (1 + w * dt);
// its velocity (useful for blur): d/dt = w^2 * dt * e^(-w dt)
const springCDv = (dt, w = 18) => dt <= 0 ? 0 : w * w * dt * Math.exp(-w * dt);
// x = lerp(from, to, springCD(t - t0))
```

Underdamped (only when overshoot is an intentional choice):

```js
function spring(dt, w = 14, zeta = 0.6) {
  if (dt <= 0) return 0;
  const wd = w * Math.sqrt(1 - zeta * zeta);
  return 1 - Math.exp(-zeta * w * dt) * (Math.cos(wd * dt) + (zeta * w / wd) * Math.sin(wd * dt));
}
```

## Camera zoom in log space

Linear interpolation of scale feels like it accelerates as it zooms in. Interpolate log(scale) so every doubling takes the same time:

```js
const zoomLerp = (s0, s1, p) => Math.exp(lerp(Math.log(s0), Math.log(s1), p));
// zoom about a focus point (fx, fy) in stage coords
function camera(el, s, fx, fy, W = 1920, H = 1080) {
  el.style.transformOrigin = '0 0';
  el.style.transform = `translate(${W / 2 - fx * s}px, ${H / 2 - fy * s}px) scale(${s})`;
}
// camera(root, zoomLerp(1, 12, expoInOut(local(t, 4, 1))), 960, 540);
```

## Velocity-based directional blur

Estimate velocity by evaluating the same pure position function a tiny step apart, then blur along the direction of travel only:

```js
function velocity(posFn, t, h = 1 / 240) {
  const a = posFn(t - h), b = posFn(t + h);
  return { vx: (b.x - a.x) / (2 * h), vy: (b.y - a.y) / (2 * h) };
}
// SVG: one feGaussianBlur per moving object; stdDeviation="sx sy" is axis-aligned,
// so rotate the object's group to the motion angle, blur on x, rotate back.
function blurFor(v, fps = 60, shutter = 0.5, maxPx = 40) {
  const px = Math.min(maxPx, Math.hypot(v.vx, v.vy) * shutter / fps);   // distance travelled while the shutter is open
  return { std: px / 2, angle: Math.atan2(v.vy, v.vx) * 180 / Math.PI };
}
// CSS fallback for mostly horizontal moves: el.style.filter = `blur(${std}px)` only when std > 0.3
```

Use this for in-scene blur on specific fast objects; use `full --sub N` for true accumulated motion blur on everything.

## Persistent-object handoff

Don't cut between shots: one object carries over and becomes the next scene.

```js
// Each shot exposes its end state; the next shot starts from it.
const SHOTS = [
  { t0: 0, t1: 2, draw: (p, el) => line(el, { x: 0, y: 540, w: lerp(0, 1920, expoOut(p)), h: 6 }) },
  { t0: 2, t1: 4, draw: (p, el) => {            // the line thickens into the text band
      const h = lerp(6, 260, springCD(p * 2)); line(el, { x: 0, y: 540 - h / 2, w: 1920, h }); } },
];
window.seek = t => { const s = SHOTS.findLast(s => t >= s.t0) || SHOTS[0]; s.draw(local(t, s.t0, s.t1 - s.t0), hero); };
```

Rules: the same DOM/SVG element keeps its identity across the handoff, and its position, size and colour are continuous at the boundary. Morph shapes by interpolating point lists that have the same point count (resample paths to N points first).

## Beat helpers

```js
const BPM = 120, SPB = 60 / BPM;
const beat = (t, bpm = BPM) => t / (60 / bpm);                  // fractional beat index
const beatTime = (n, bpm = BPM) => n * 60 / bpm;               // beat index -> seconds
const bar = (t, bpm = BPM, per = 4) => Math.floor(beat(t, bpm) / per);
// 1 right on the beat, falling to 0 over `width` beats: pulses, flashes, scale punches
const onBeat = (t, bpm = BPM, width = 0.25) => { const f = beat(t, bpm) % 1; return f < width ? 1 - f / width : 0; };
// progress of the move that starts on beat n and lasts len beats
const beatEvent = (t, n, len = 1, bpm = BPM) => local(t, beatTime(n, bpm), len * 60 / bpm);
// x = lerp(a, b, expoOut(beatEvent(t, 8, 0.5)))
```

## Stagger / word reveal

```js
words.forEach((w, i) => {
  const p = expoOut(local(t, start + i * 0.08, 0.35));
  w.style.opacity = p;
  w.style.transform = `translateY(${(1 - p) * 40}px)`;
});
```
