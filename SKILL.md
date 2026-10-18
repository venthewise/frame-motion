---
name: frame-motion
description: >-
  Create deterministic motion-graphics videos with pure seek(t) HTML +
  Playwright frame capture + ffmpeg. Use for explainers, kinetic typography,
  animated charts, product walkthroughs, launch films, or any short motion
  design built from scratch where every frame comes from code (plus offline
  Web Audio soundtracks). Not for editing real footage (trim, overlay, caption),
  realistic or talking-head video.
license: MIT (see LICENSE)
compatibility: Requires Python 3.10+ with playwright, pillow and numpy, Playwright Chromium, and ffmpeg + ffprobe on PATH. Works offline.
metadata:
  author: Paolo Libertad (github.com/venthewise)
  version: "3.0.0"
---
# Frame Motion: deterministic code-only motion design

Every frame is a pure function of time. One `window.seek(t)` draws the whole frame; Playwright captures it and streams it into ffmpeg. No server (`file://`), no temp frame folders, one generic CLI.

Inspired by Anthropic's Claude Motion; an independent project, not affiliated with Anthropic.

## When to use this vs Remotion

- **frame-motion (this skill):** animation built from scratch where every frame comes from code (HTML/SVG/Canvas/WebGL driven by `seek(t)`), plus soundtracks synthesized offline with Web Audio (`audio render`). Kinetic type, explainers, animated charts, showreels, loops.
- **A Remotion skill / Remotion project** (if you have one): trimming, cutting, overlaying and captioning **real footage** (screen recordings, camera clips, talking heads), adding music to an edit.
- **Hybrid:** render the graphics here (`full` → MP4 inserts or graphics plates), then composite them over or between the footage in Remotion (`<Video>` / `<OffthreadVideo>` + `<Sequence>`), with captions and audio done in Remotion.

## Scene contract

```js
window.seek = async function (t) {   // t in seconds; may be sync or async (renderer awaits it)
  // draw the ENTIRE frame for exactly t. No Date, no rAF loops, no timers, no state from the previous frame.
};
window.ready = true;                   // after fonts / images / data are loaded
```

- Put the canvas in `<div id="stage">` (or `<svg id="stage">`); the renderer captures only `#stage` if present, else the viewport.
- Randomness: seeded PRNG only (`mulberry32(seed)` in `references/motion-patterns.md`). Never `Math.random`.
- Fonts/images local or data-URI. Page must work offline.
- The renderer freezes stray motion: injects `transition:none; animation-play-state:paused` on everything and pauses `document.getAnimations()`. Pass `--no-harden` only if a scene really depends on CSS animation state.

### Driving libraries from seek(t)

| Library | Inside `seek(t)` |
|---|---|
| GSAP | build `tl` once with `paused:true` (or `tl.pause()`), then `tl.seek(t)` |
| Web Animations API | `anim.pause()` once, then `anim.currentTime = t * 1000` |
| Lottie (lottie-web) | `anim.goToAndStop(t * 1000, false)` (ms) |
| anime.js | `autoplay:false`, then `anim.seek(t * 1000)` |
| Canvas 2D / WebGL | clear + redraw everything from `t`; WebGL: `preserveDrawingBuffer:true`, render with `--webgl` |
| SVG | set attributes (`d`, `transform`, `stroke-dashoffset`) computed from `t` |
| Video element | `v.currentTime = t; await new Promise(r => v.onseeked = r)` (async seek is supported) |

## Workflow

1. **Brief.** Duration, aspect (`--preset 16:9|9:16|4:5|1:1`; default 16:9 1920x1080, pass `--preset 9:16` for vertical on EVERY command), message, audience. Name the concept in one line and commit to it.
2. **Beat map.** Pick a BPM (beat = 60/BPM s). Write a table: beat #, time, one event per beat. Scene changes land on downbeats. Last full bar = strongest moment.
3. **Four key stills approved.** Build the scene, `probe` 4 frames (hook, middle, peak, end) and check them before anything else.
4. **Draft.** `draft` (half-res, 30fps, JPEG) to judge rhythm, not sharpness.
5. **Checks.** `beats` sheet (one frame per beat, look at it), `pops` (on the draft, with `--bpm`), `loop` if it loops, `determinism`.
6. **Critique loop** (below). Iterate until every score is 8+.
7. **Final render.** `full` with `--sub 3..8 --shutter 0.5` for motion blur, or `parallel` to split across CPU cores. Add `--cuts` at hard cuts.
8. **Audio.** Synthesize in code (`audio render`, see `examples/audio-template.html`) or use licensed music/SFX. Place SFX by measured peak (`audio peak --align`), find music drops with `audio drop`, normalise to -14 LUFS.
9. **Mux.** `mux --lufs -14 [--sfx file@t:gain ...]`.
10. **Deliver.** Final `sheet` of the MP4 (every 1s) + `poster` thumbnail (frame 0 or a chosen t).

## Critique loop

Score the beats sheet and final contact sheet 1-10 on each:

| Axis | 8+ means |
|---|---|
| Hook | first 0.5-1s is legible and makes you stay |
| Readability | every word on screen long enough to read at phone size |
| Motion quality | intentional easing, blur follows speed, no jitter or pops |
| Continuity | each shot is built from the previous one, objects hand off |
| Rhythm / beat sync | something lands on every beat, scene changes on downbeats |
| Originality | would not be mistaken for a template |
| Loop / ending | end title lands; loop seam matches position and velocity |

Write the scores and the single biggest fix, fix it, re-render the affected sheet, re-score. Stop only when all axes are 8+. Optional: hand the sheets and the beat map to a separate read-only reviewer that only scores and lists fixes, never edits.

## Anti-generic rules

- One concept, one typeface, at most 3 colours + black/white, one camera language.
- No default gradients, glows, particle bursts, lens flares, camera shake, RGB split, crossfades, or stock template layouts (centered title + subtitle + logo bug).
- No bouncy overshoot unless it's an intentional character choice; default to critically damped springs and exponential eases.
- No holds longer than ~0.5s in short pieces. Persistent objects morph into the next scene instead of cutting.
- Label illustrative numbers; never invent claims.

## Commands

`R=scripts/render.py`, resolved relative to the folder this SKILL.md is in (the skill directory), wherever the skill is installed; e.g. `R="<skill dir>/scripts/render.py"`. Run `python3 $R <cmd> -h` for all flags (`python` on Windows). Example paths below (`examples/...`) are relative to the skill directory too.

| Command | What it does | Example |
|---|---|---|
| `probe` | stills named by time `t_01.50s.png` | `python3 $R probe scene.html 0.5 4 8 --preset 16:9 --out probe` |
| `poster` | one still / thumbnail | `python3 $R poster scene.html poster.png --t 0 --preset 16:9` |
| `sheet` | tiled contact sheet with time labels, from a scene **or a video** | `python3 $R sheet out.mp4 --every 1 --duration 15 --out sheet.png` |
| `beats` | one labelled frame per beat + sheet | `python3 $R beats scene.html --bpm 120 --duration 15 --out beats` |
| `draft` | half-res (device scale 0.5, same layout), 30fps, JPEG, crf 23 | `python3 $R draft scene.html draft.mp4 --duration 15 --preset 16:9` |
| `full` | final render, streamed to ffmpeg | `python3 $R full scene.html out.mp4 --fps 60 --duration 15 --sub 3 --shutter 0.5 --preset 16:9` |
| `parallel` | `full` split over N browser processes, joined losslessly (concat copy), frame count verified | `python3 $R parallel scene.html out.mp4 --fps 60 --duration 15 --sub 3 --jobs 5 --preset 16:9` |
| `pops` | frame-diff spikes (> threshold x neighbour median) + single-frame flashes; `--bpm` tags on-beat / half-beat / OFF-BEAT | `python3 $R pops draft.mp4 --bpm 120` |
| `loop` | seam check: frame 0 vs t=D (position) and first vs last step (velocity) | `python3 $R loop scene.html --duration 15 --fps 60 --preset 16:9` |
| `determinism` | seek a, b, a and require pixel-identical captures of a | `python3 $R determinism scene.html --times 3.7,11.2` |
| `audio render` | runs `window.renderAudio(duration, rate)` (OfflineAudioContext, returns AudioBuffer) -> 16/24-bit WAV | `python3 $R audio render audio.html music.wav --duration 15` |
| `audio peak` | time of the peak + onset; `--align T` prints where to place the file so the peak hits T | `python3 $R audio peak whoosh.wav --align 6.0` |
| `audio drop` | biggest low-band (<150 Hz) energy jump = the drop | `python3 $R audio drop music.wav` |
| `audio loudnorm` | two-pass: measure, then exact gain (or gain + limiter) to target, true-peak ceiling -1 dBTP | `python3 $R audio loudnorm in.wav out.wav --lufs -14` |
| `mux` | video copy + AAC; `--offset`, `--volume`, repeatable `--sfx file@t[:gain]`, `--lufs -14` (re-limits if AAC overshoots the ceiling) | `python3 $R mux out.mp4 music.wav final.mp4 --lufs -14 --sfx hit.wav@2.0:0.7` |

Shared scene flags: `--preset 16:9|9:16|4:5|1:1` (1920x1080 default, 1080x1920, 1080x1350, 1080x1080), `--width/--height` override, `--device-scale`, `--webgl` (SwiftShader ANGLE so WebGL/three.js isn't black headless), `--no-harden`.
Video flags (`full`, `draft`, `parallel`): `--fps` (30), `--duration`, `--start` (chunk start), `--jpeg [--quality 92]`, `--crf` (16), `--sub N` (1 = no blur), `--shutter S` (0.5 = 180°), `--align start|center`, `--cuts t1,t2`, `--x264-preset`, `--x264-threads`.

**Default size is 1920x1080 (16:9)** when no `--preset`/`--width`/`--height` is given. For vertical output pass `--preset 9:16` (on probe, draft, full, checks alike, so layouts match). Every scene command prints the size it uses first, e.g. `frame-motion: full at 1920x1080 (default, = --preset 16:9)`; `draft` also prints its half-res output pixels (960x540 for the default).

## How capture works

- Chromium flags: `--force-color-profile=srgb --disable-gpu-vsync --font-render-hinting=none --disable-lcd-text --hide-scrollbars` (+ SwiftShader with `--webgl`).
- Waits for `window.ready` and `document.fonts.ready`, injects the freeze CSS, then per capture runs `await window.seek(t)`, pauses running animations, waits 2 rAFs, screenshots.
- Motion blur: subframe times `t = frame/fps + k*shutter/(fps*sub)` (`--align center` centres them), decoded and averaged in numpy, then streamed as raw RGB to ffmpeg. A frame next to a `--cuts` time only samples its own side of the cut.
- Encode: libx264, yuv420p, converted to BT.709 TV range and tagged (`colorspace/primaries/trc bt709, range tv`), crf 16, `+faststart`.

Speed on an 8-core Linux machine: 1920x1080 SVG showreel, 60fps, 3 subframes, `parallel --jobs 5`: 900 frames (2700 captures) in about 60s. Heavy DOM scenes with large blurred or gradient layers run around 3-4 captures/s per process. Always probe/draft first.

## Setup (one time)

```bash
pip install playwright pillow numpy && python3 -m playwright install chromium
ffmpeg -version
```

## Common issues

| Problem | Fix |
|---|---|
| Frame shows undrawn/old state | make `seek` return its Promise (`async`); renderer awaits it |
| `determinism` FAIL | state leaks between frames: reset everything inside `seek`, no accumulation |
| WebGL / three.js black | `--webgl`, and `preserveDrawingBuffer: true` |
| `loop` velocity FAIL | the end holds still while the start moves (or vice versa): ease both sides of the seam with the same velocity |
| `pops` spikes | on-beat spikes are usually intentional hits; check OFF-BEAT ones and FLASH frames in a `sheet` |
| Text missing | set `window.ready` only after fonts load |
| Parallel runs out of RAM | lower `--jobs` (each job = 1 Chromium + 1 x264) |

## Files

- `scripts/render.py`: renderer + checks + audio tools (module docstring = quick reference)
- `examples/kinetic-title.html`: 4 s 16:9 "NIGHT TRAIN" kinetic title at 120 BPM that follows the anti-generic rules (one typeface, one accent, asymmetric left-weighted single baseline, persistent rule handoff, critically damped springs, lateral camera; no glow, no centred stack). Copy its structure, not its look
- `examples/audio-template.html`: BPM-parameterised kick / hit / riser / bass synth for `audio render`
- `references/motion-patterns.md`: easing, springs, camera, blur, handoff, beat helpers
