#!/usr/bin/env python3
"""frame-motion renderer: HTML seek(t) -> Playwright capture -> ffmpeg MP4.

Scene contract: window.seek(t) (t in seconds; may be async / return a Promise) and
window.ready = true once assets are loaded. The renderer waits for window.ready AND
document.fonts.ready, freezes stray CSS/Web animations, then per capture does
`await window.seek(t)` + 2 requestAnimationFrames before the screenshot. If the page
has a #stage element, only its box is captured; otherwise the full viewport.

Run `render.py <command> -h` for every flag. Commands:

  probe   HTML T [T ...] [--out DIR]                    stills named t_01.50s.png
  poster  HTML OUT.png [--t 0]                          single still (thumbnail)
  sheet   HTML|VIDEO --times a,b,c | --every S --duration D [--cols 4] [--out sheet.png]
  beats   HTML --bpm 120 --duration 15 [--offset 0] [--out beats]   one labelled frame per beat
  full    HTML OUT.mp4 --duration D [--fps 30] [--sub N --shutter 0.5 --cuts t1,t2] [--start S]
          [--jpeg] [--crf 16] [--preset 16:9|9:16|4:5|1:1] [--width W --height H] [--webgl]
  draft   HTML OUT.mp4 --duration D                     half-res, 30fps, JPEG, fast
  parallel HTML OUT.mp4 --duration D --jobs N [all full flags]   N browser processes, lossless join
  pops    VIDEO|HTML [--threshold 3]                    flag frame-diff spikes and 1-frame flashes
  loop    HTML --duration D [--fps 30]                  seam check: position (0 vs D) + velocity
  determinism HTML --times a,b                          seek a, b, a -> both captures of a identical?
  audio render AUDIO.html OUT.wav --duration D [--rate 48000] [--bits 16|24]
  audio peak FILE.wav [--align T]                       time of the peak / transient
  audio drop MUSIC.wav                                  biggest low-band energy jump
  audio loudnorm IN.wav OUT.wav [--lufs -14] [--tp -1]  two-pass EBU R128
  mux     VIDEO AUDIO OUT.mp4 [--offset S] [--volume V] [--sfx file@t[:gain] ...] [--lufs -14]

Size: default 1920x1080 (16:9) when no --preset/--width/--height is given (pass
--preset 9:16 for vertical). --preset sets
16:9=1920x1080, 9:16=1080x1920, 4:5=1080x1350, 1:1=1080x1080; explicit --width/--height
override it. Every scene command prints the chosen WxH before it starts.
Encode: libx264, yuv420p, BT.709 TV range (converted + tagged), crf 16, +faststart.
Requires: pip install playwright pillow numpy; python3 -m playwright install chromium; ffmpeg.
"""
import argparse
import asyncio
import base64
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

PRESETS = {"16:9": (1920, 1080), "9:16": (1080, 1920), "4:5": (1080, 1350), "1:1": (1080, 1080)}
CHROME_ARGS = ["--force-color-profile=srgb", "--disable-gpu-vsync", "--font-render-hinting=none",
               "--disable-lcd-text", "--hide-scrollbars", "--disable-background-timer-throttling",
               "--disable-renderer-backgrounding", "--autoplay-policy=no-user-gesture-required"]
WEBGL_ARGS = ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
              "--ignore-gpu-blocklist", "--enable-webgl"]
FREEZE_CSS = ("*,*::before,*::after{transition:none!important;animation-play-state:paused!important;"
              "caret-color:transparent!important}")
# one round trip per capture: await the (possibly async) seek, pause any Web Animations, 2 paints
CAPTURE_JS = """async (a) => {
  await window.seek(a.t);
  if (a.freeze && document.getAnimations) for (const an of document.getAnimations()) if (an.playState === 'running') an.pause();
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  return true;
}"""
VIDEO_EXT = {".mp4", ".mov", ".webm", ".mkv", ".m4v", ".gif"}
COLOR_OUT = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]
VF_709 = ("scale=trunc(iw/2)*2:trunc(ih/2)*2:out_color_matrix=bt709:out_range=tv:flags=bicubic,format=yuv420p,"
          "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv")


def die(msg):
    sys.exit(f"error: {msg}")


def need_ffmpeg():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        die("ffmpeg/ffprobe not found on PATH")


def floats(s):
    return [float(x) for x in re.split(r"[,\s]+", s.strip()) if x] if s else []


DEFAULT_PRESET = "16:9"  # 1920x1080
SCENE_CMDS = {"probe", "poster", "beats", "full", "draft", "parallel", "loop", "determinism"}


def resolve_size(a):
    preset = getattr(a, "preset", None)
    w, h = PRESETS[preset or DEFAULT_PRESET]
    if a.width or a.height:
        why = "--width/--height" + (f" over --preset {preset}" if preset else "")
    else:
        why = f"--preset {preset}" if preset else f"default, = --preset {DEFAULT_PRESET}"
    a.width = a.width or w
    a.height = a.height or h
    renders_scene = a.cmd in SCENE_CMDS or (a.cmd == "sheet" and Path(a.src).suffix.lower() not in VIDEO_EXT)
    if renders_scene:
        print(f"frame-motion: {a.cmd} at {a.width}x{a.height} ({why})", flush=True)


# ---------------------------------------------------------------- browser
async def open_scene(p, html, width, height, scale=1.0, webgl=False, harden=True, timeout=30000):
    args = CHROME_ARGS + (WEBGL_ARGS if webgl else [])
    browser = await p.chromium.launch(args=args)
    page = await browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=scale)
    page.on("pageerror", lambda e: print(f"[page error] {e}", file=sys.stderr))
    page.on("console", lambda m: print(f"[console.{m.type}] {m.text}", file=sys.stderr)
            if m.type in ("error", "warning") and "GL Driver Message" not in m.text else None)
    await page.goto(Path(html).resolve().as_uri())
    try:
        await page.wait_for_function("() => window.ready === true && typeof window.seek === 'function'",
                                     timeout=timeout)
    except Exception:
        await browser.close()
        die(f"scene never set window.ready = true / window.seek missing ({timeout/1000:.0f}s timeout)")
    await page.evaluate("() => document.fonts.ready.then(() => true)")
    if harden:
        await page.add_style_tag(content=FREEZE_CSS)
        await page.evaluate("() => { if (document.getAnimations) document.getAnimations().forEach(a => a.pause()); }")
    clip = await page.evaluate("""() => { const s = document.getElementById('stage'); if (!s) return null;
        const r = s.getBoundingClientRect(); return {x: r.left, y: r.top, width: r.width, height: r.height}; }""")
    if clip and (clip["width"] < 2 or clip["height"] < 2):
        clip = None
    shot = {"clip": clip} if clip else {}
    page._fm = {"shot": shot, "freeze": harden}
    return browser, page


async def grab(page, t, fmt="png", quality=92, path=None):
    await page.evaluate(CAPTURE_JS, {"t": float(t), "freeze": page._fm["freeze"]})
    kw = dict(page._fm["shot"], type=fmt)
    if fmt == "jpeg":
        kw["quality"] = quality
    if path:
        kw["path"] = str(path)
    return await page.screenshot(**kw)


def decode(b):
    import numpy as np
    from PIL import Image
    return np.asarray(Image.open(io.BytesIO(b)).convert("RGB"))


def pw():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        die("install playwright: pip install playwright && python3 -m playwright install chromium")
    return async_playwright


async def capture_times(a, times, scale=None, fmt="png"):
    """Return list of RGB numpy arrays for the given times (one browser)."""
    async with pw()() as p:
        b, pg = await open_scene(p, a.html, a.width, a.height, scale or a.device_scale, a.webgl, not a.no_harden)
        out = [decode(await grab(pg, t, fmt)) for t in times]
        await b.close()
    return out


# ---------------------------------------------------------------- stills
async def probe(a):
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    async with pw()() as p:
        b, pg = await open_scene(p, a.html, a.width, a.height, a.device_scale, a.webgl, not a.no_harden)
        for t in a.times:
            path = out / f"t_{t:05.2f}s.png"
            await grab(pg, t, path=path)
            print(path)
        await b.close()


async def poster(a):
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    async with pw()() as p:
        b, pg = await open_scene(p, a.html, a.width, a.height, a.device_scale, a.webgl, not a.no_harden)
        await grab(pg, a.t, fmt="jpeg" if a.out.lower().endswith((".jpg", ".jpeg")) else "png",
                   quality=95, path=a.out)
        await b.close()
    print(f"Wrote {a.out} (t={a.t}s)")


def font(size):
    from PIL import ImageFont
    for f in ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",   # Linux
              "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
              "/System/Library/Fonts/Menlo.ttc",                           # macOS
              "C:/Windows/Fonts/consolab.ttf", "C:/Windows/Fonts/consola.ttf"):  # Windows
        if os.path.exists(f):
            return ImageFont.truetype(f, size)
    return ImageFont.load_default(size=size)


def tile(images, labels, cols, thumb_w, out):
    from PIL import Image, ImageDraw
    ims = [Image.fromarray(x) for x in images]
    w = thumb_w
    h = round(ims[0].height * w / ims[0].width)
    lab = max(22, w // 14)
    pad = 8
    rows = -(-len(ims) // cols)
    sheet = Image.new("RGB", (pad + cols * (w + pad), pad + rows * (h + lab + pad)), (24, 24, 24))
    d = ImageDraw.Draw(sheet)
    f = font(int(lab * 0.72))
    for i, (im, txt) in enumerate(zip(ims, labels)):
        x = pad + (i % cols) * (w + pad)
        y = pad + (i // cols) * (h + lab + pad)
        sheet.paste(im.resize((w, h), Image.LANCZOS), (x, y + lab))
        d.text((x + 4, y + 2), txt, fill=(235, 235, 235), font=f)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    return sheet.size


def video_frames_at(video, times):
    import numpy as np
    w, h = probe_video(video)[:2]
    res = []
    for t in times:
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.4f}", "-i", video, "-frames:v", "1",
                              "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
        if len(raw) < w * h * 3:  # past the end: take the last frame
            raw = subprocess.run(["ffmpeg", "-v", "error", "-sseof", "-0.1", "-i", video, "-frames:v", "1",
                                  "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
        res.append(np.frombuffer(raw[:w * h * 3], np.uint8).reshape(h, w, 3))
    return res


async def sheet_cmd(a):
    if a.times:
        times = floats(a.times)
    elif a.every and a.duration:
        n = int(math.floor(a.duration / a.every + 1e-9))
        times = [round(i * a.every, 4) for i in range(n + 1)]
    else:
        die("sheet needs --times a,b,c or --every S --duration D")
    if Path(a.src).suffix.lower() in VIDEO_EXT:
        imgs = video_frames_at(a.src, times)
    else:
        a.html = a.src
        imgs = await capture_times(a, times)
    size = tile(imgs, [f"{t:.2f}s" for t in times], a.cols, a.thumb, a.out)
    print(f"Wrote {a.out} ({len(times)} frames, {size[0]}x{size[1]})")


async def beats(a):
    spb = 60.0 / a.bpm
    times, k = [], 0
    while a.offset + k * spb <= a.duration + 1e-9:
        times.append(round(a.offset + k * spb, 4))
        k += 1
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    imgs = []
    async with pw()() as p:
        b, pg = await open_scene(p, a.html, a.width, a.height, a.device_scale, a.webgl, not a.no_harden)
        for i, t in enumerate(times):
            png = await grab(pg, min(t, a.duration))
            (out / f"beat_{i+1:03d}_t_{t:05.2f}s.png").write_bytes(png)
            imgs.append(decode(png))
        await b.close()
    labels = [f"#{i+1} bar{i//a.per_bar+1}.{i%a.per_bar+1} {t:.2f}s" for i, t in enumerate(times)]
    sp = out / "beats_sheet.png"
    size = tile(imgs, labels, a.cols or a.per_bar * 2, a.thumb, sp)
    print(f"{len(times)} beats @ {a.bpm} BPM ({spb:.3f}s) -> {out}/beat_*.png + {sp} ({size[0]}x{size[1]})")


# ---------------------------------------------------------------- video
def sub_times(tf, fps, sub, shutter, cuts, align, tmax):
    if sub <= 1:
        return [tf]
    span = shutter / fps
    lo = tf - span / 2 if align == "center" else tf
    hi = lo + span
    for c in cuts:  # never straddle a hard cut: keep samples on the frame's own side
        if lo < c <= hi or lo <= c < hi:
            if tf < c:
                hi = min(hi, c - 1e-5)
            else:
                lo = max(lo, c)
    step = (hi - lo) / sub
    ts = [lo + k * step for k in range(sub)] if align != "center" else [lo + (k + 0.5) * step for k in range(sub)]
    return [min(max(0.0, t), tmax) for t in ts]


def x264_cmd(out, fps, crf, src, w=None, h=None, xpreset="medium", threads=0):
    if src == "raw":
        inp = ["-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-framerate", str(fps), "-i", "-"]
    else:
        inp = ["-f", "image2pipe", "-framerate", str(fps), "-i", "-"]
    return (["ffmpeg", "-y", "-loglevel", "error"] + inp +
            ["-vf", VF_709, "-c:v", "libx264", "-preset", xpreset, "-crf", str(crf), "-pix_fmt", "yuv420p"] +
            (["-threads", str(threads)] if threads else []) +
            COLOR_OUT + ["-movflags", "+faststart", "-r", str(fps), out])


async def full(a):
    import numpy as np
    from concurrent.futures import ThreadPoolExecutor
    need_ffmpeg()
    fps = a.fps
    frames = a.frames if a.frames else round(a.duration * fps)
    first = round(a.start * fps)
    end_t = a.total if a.total else (first + frames) / fps
    cuts = floats(a.cuts)
    fmt = "jpeg" if a.jpeg else "png"
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    blur = a.sub > 1
    t0 = time.time()
    ff = None
    pool = ThreadPoolExecutor(max_workers=3)
    loop = asyncio.get_running_loop()
    try:
        async with pw()() as p:
            browser, page = await open_scene(p, a.html, a.width, a.height, a.device_scale, a.webgl, not a.no_harden)
            for i in range(frames):
                tf = (first + i) / fps
                if not blur:
                    data = await grab(page, tf, fmt, a.quality)
                    if ff is None:
                        ff = subprocess.Popen(x264_cmd(a.out, fps, a.crf, "pipe", xpreset=a.x264_preset,
                                                       threads=a.x264_threads),
                                              stdin=subprocess.PIPE)
                    ff.stdin.write(data)
                else:
                    ts = sub_times(tf, fps, a.sub, a.shutter, cuts, a.align, max(0.0, end_t - 1e-4))
                    futs = []
                    for t in ts:
                        futs.append(loop.run_in_executor(pool, decode, await grab(page, t, fmt, a.quality)))
                    acc = None
                    for fu in futs:
                        arr = await fu
                        acc = arr.astype(np.uint32) if acc is None else acc + arr
                    avg = ((acc + len(ts) // 2) // len(ts)).astype(np.uint8)
                    if ff is None:
                        hh, ww = avg.shape[:2]
                        ff = subprocess.Popen(x264_cmd(a.out, fps, a.crf, "raw", ww, hh, a.x264_preset,
                                                       a.x264_threads),
                                              stdin=subprocess.PIPE)
                    ff.stdin.write(avg.tobytes())
                if (i + 1) % fps == 0 or i + 1 == frames:
                    el = time.time() - t0
                    print(f"  {(i+1)/fps:6.2f}s / {frames/fps:.2f}s  ({i+1}/{frames} frames, "
                          f"{el:.1f}s elapsed, {(i+1)/el:.2f} fps)", flush=True)
            await browser.close()
    finally:
        pool.shutdown()
        if ff:
            ff.stdin.close()
            rc = ff.wait()
            if rc != 0:
                die(f"ffmpeg failed (exit {rc})")
    print(f"Wrote {a.out}: {frames} frames @ {fps}fps, sub={a.sub} shutter={a.shutter} in {time.time()-t0:.1f}s")


async def draft(a):
    a.fps, a.jpeg, a.sub, a.frames, a.total = a.fps or 30, True, 1, 0, 0
    a.device_scale = 0.5 * a.device_scale  # same CSS layout, half the pixels
    print(f"frame-motion: draft layout {a.width}x{a.height}, output pixels "
          f"{round(a.width * a.device_scale)}x{round(a.height * a.device_scale)} (device scale {a.device_scale:g})", flush=True)
    a.crf = a.crf or 23
    a.x264_preset = "veryfast"
    await full(a)


def ffprobe_frames(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
                        "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", path],
                       capture_output=True, text=True)
    return int(r.stdout.strip() or 0)


def parallel(a):
    need_ffmpeg()
    total = round(a.duration * a.fps)
    first = round(a.start * a.fps)
    jobs = max(1, min(a.jobs, total))
    base, rem = divmod(total, jobs)
    work = Path(tempfile.mkdtemp(prefix="fm-par-", dir=Path(a.out).resolve().parent))
    passthru = ["--fps", str(a.fps), "--width", str(a.width), "--height", str(a.height),
                "--device-scale", str(a.device_scale), "--crf", str(a.crf), "--sub", str(a.sub),
                "--shutter", str(a.shutter), "--align", a.align, "--quality", str(a.quality),
                "--x264-preset", a.x264_preset,
                "--x264-threads", str(a.x264_threads or max(2, (os.cpu_count() or 4) // jobs)), "--total", str((first + total) / a.fps)]
    if a.cuts:
        passthru += ["--cuts", a.cuts]
    for flag in ("jpeg", "webgl", "no_harden"):
        if getattr(a, flag):
            passthru.append("--" + flag.replace("_", "-"))
    procs, chunks, f0 = [], [], first
    t0 = time.time()
    for j in range(jobs):
        n = base + (1 if j < rem else 0)
        c = work / f"chunk_{j:02d}.mp4"
        log = open(work / f"chunk_{j:02d}.log", "w")
        cmd = [sys.executable, __file__, "full", a.html, str(c), "--start", repr(f0 / a.fps),
               "--frames", str(n), "--duration", repr(n / a.fps)] + passthru
        procs.append((subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT), log, n))
        chunks.append(c)
        f0 += n
    print(f"parallel: {total} frames in {jobs} chunks -> {work}", flush=True)
    while any(pr.poll() is None for pr, _, _ in procs):
        time.sleep(5)
        done = 0
        for j, (_, _, n) in enumerate(procs):
            txt = (work / f"chunk_{j:02d}.log").read_text(errors="ignore")
            m = re.findall(r"\((\d+)/\d+ frames", txt)
            done += int(m[-1]) if m else 0
        el = time.time() - t0
        print(f"  {done}/{total} frames, {el:.0f}s elapsed", flush=True)
    for j, (pr, log, n) in enumerate(procs):
        log.close()
        if pr.returncode != 0:
            die(f"chunk {j} failed, see {work}/chunk_{j:02d}.log")
        got = ffprobe_frames(str(chunks[j]))
        if got != n:
            die(f"chunk {j} has {got} frames, expected {n}")
    lst = work / "list.txt"
    lst.write_text("".join(f"file '{c.name}'\n" for c in chunks))
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-c", "copy", "-movflags", "+faststart", str(Path(a.out).resolve())], check=True)
    got = ffprobe_frames(a.out)
    if got != total:
        die(f"joined file has {got} frames, expected {total} (chunks kept in {work})")
    if not a.keep_chunks:
        shutil.rmtree(work)
    print(f"Wrote {a.out}: {got} frames @ {a.fps}fps from {jobs} chunks in {time.time()-t0:.1f}s")


# ---------------------------------------------------------------- checks
def probe_video(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height,r_frame_rate", "-of", "json", path], capture_output=True, text=True)
    s = json.loads(r.stdout)["streams"][0]
    n, d = s["r_frame_rate"].split("/")
    return s["width"], s["height"], float(n) / float(d)


def gray_small(arr, w=192):
    import numpy as np
    from PIL import Image
    im = Image.fromarray(arr).convert("L")
    return np.asarray(im.resize((w, max(2, round(im.height * w / im.width))), Image.BILINEAR), np.float32)


def mad(x, y):
    import numpy as np
    return float(np.mean(np.abs(x.astype(np.float32) - y.astype(np.float32))))


async def pops(a):
    import numpy as np
    if Path(a.src).suffix.lower() in VIDEO_EXT:
        w, h, fps = probe_video(a.src)
        sw = 192
        sh = max(2, round(h * sw / w / 2) * 2)
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", a.src, "-vf", f"scale={sw}:{sh},format=gray",
                              "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
        fr = np.frombuffer(raw, np.uint8).reshape(-1, sh, sw).astype(np.float32)
    else:
        if not a.duration:
            die("pops on an HTML scene needs --duration")
        fps = a.fps or 30
        a.html = a.src
        n = round(a.duration * fps)
        fr = [gray_small(x) for x in await capture_times(a, [i / fps for i in range(n)], scale=0.25, fmt="jpeg")]
        fr = np.stack(fr)
    n = len(fr)
    d = np.zeros(n)
    d[1:] = np.mean(np.abs(np.diff(fr, axis=0)), axis=(1, 2))
    flagged = []
    for i in range(1, n):
        nb = np.concatenate([d[max(1, i - a.window):i], d[i + 1:i + 1 + a.window]])
        med = float(np.median(nb)) if len(nb) else 0.0
        if d[i] >= a.floor and d[i] > a.threshold * max(med, a.min_median):
            flagged.append((i, "spike", d[i], med))
    flashes = []
    for i in range(1, n - 1):
        if d[i] >= a.floor and d[i + 1] >= a.floor:
            back = mad(fr[i - 1], fr[i + 1])
            if back < 0.35 * min(d[i], d[i + 1]):
                flashes.append((i, d[i], d[i + 1], back))
    cuts = floats(a.cuts)
    def at_cut(i):
        return any(abs(i / fps - c) <= 1.5 / fps for c in cuts)
    flagged = [f for f in flagged if not at_cut(f[0])]
    flashes = [f for f in flashes if not at_cut(f[0])]
    def tag(i):
        if not a.bpm:
            return ""
        spb = 60.0 / a.bpm
        k = round((i / fps - a.offset) / spb)
        if abs(i / fps - (a.offset + k * spb)) <= a.beat_tol / fps:
            return f"  on beat {k+1}"
        h = round((i / fps - a.offset) / (spb / 2))
        if abs(i / fps - (a.offset + h * spb / 2)) <= a.beat_tol / fps:
            return f"  on half-beat {h/2+1:.1f}"
        return "  OFF-BEAT"
    print(f"pops: {n} frames @ {fps:g}fps, median diff {np.median(d[1:]):.2f}, max {d.max():.2f} "
          f"(threshold {a.threshold}x neighbour median, floor {a.floor})")
    for i, kind, v, med in flagged:
        print(f"  SPIKE  frame {i:5d}  t={i/fps:7.3f}s  diff {v:6.2f}  vs neighbour median {med:5.2f}{tag(i)}")
    for i, v1, v2, back in flashes:
        print(f"  FLASH  frame {i:5d}  t={i/fps:7.3f}s  in {v1:6.2f} out {v2:6.2f} (prev vs next {back:.2f}){tag(i)}")
    if a.bpm:
        off = [f for f in flagged if tag(f[0]) == "  OFF-BEAT"]
        half = [f for f in flagged if "half" in tag(f[0])]
        print(f"beat sync: {len(flagged)-len(off)-len(half)} on a beat, {len(half)} on a half-beat, {len(off)} off-beat "
              f"(of {len(flagged)} spikes, tolerance {a.beat_tol:g} frames)")
    print(f"RESULT: {len(flagged)} spike(s), {len(flashes)} single-frame flash(es)"
          + (" -> review those timestamps (intentional beat hits are fine)" if flagged or flashes else " -> clean"))


async def loop_cmd(a):
    fps, D = a.fps, a.duration
    dt = 1.0 / fps
    f0, f1, fa, fb = await capture_times(a, [0.0, dt, D - dt, D])
    import numpy as np
    pos_mean = mad(f0, fb)
    pos_max = float(np.max(np.abs(f0.astype(np.int16) - fb.astype(np.int16))))
    v_start, v_end = mad(f0, f1), mad(fa, fb)
    vr = (max(v_start, v_end) + 1e-3) / (min(v_start, v_end) + 1e-3)
    pos_ok = pos_mean <= a.tol
    vel_ok = (max(v_start, v_end) < 0.5) or vr <= a.vel_ratio
    print(f"loop: frame 0 vs t={D}s  mean diff {pos_mean:.3f}  max {pos_max:.0f}  -> {'PASS' if pos_ok else 'FAIL'} "
          f"(tol {a.tol})")
    print(f"      velocity: first step {v_start:.3f}  last step {v_end:.3f}  ratio {vr:.2f} -> "
          f"{'PASS' if vel_ok else 'FAIL'} (max ratio {a.vel_ratio})")
    print("RESULT:", "PASS" if pos_ok and vel_ok else "FAIL")


async def determinism(a):
    import numpy as np
    ts = floats(a.times)
    if len(ts) < 2:
        die("--times needs two values a,b")
    ta, tb = ts[0], ts[1]
    x1, _, x2 = await capture_times(a, [ta, tb, ta])
    mx = float(np.max(np.abs(x1.astype(np.int16) - x2.astype(np.int16))))
    mean = mad(x1, x2)
    ok = mx == 0
    print(f"determinism: seek {ta} -> {tb} -> {ta}: max diff {mx:.0f}, mean {mean:.4f} -> "
          f"{'PASS (pixel identical)' if ok else 'FAIL: state leaks between frames'}")


# ---------------------------------------------------------------- audio
AUDIO_JS = """async (a) => {
  if (typeof window.renderAudio !== 'function') throw new Error('page has no window.renderAudio(duration, rate)');
  const buf = await window.renderAudio(a.duration, a.rate);
  const out = [];
  for (let c = 0; c < buf.numberOfChannels; c++) {
    const u8 = new Uint8Array(buf.getChannelData(c).slice().buffer); let s = '';
    for (let i = 0; i < u8.length; i += 0x8000) s += String.fromCharCode.apply(null, u8.subarray(i, i + 0x8000));
    out.push(btoa(s));
  }
  return {rate: buf.sampleRate, ch: out};
}"""


def write_wav(path, data, rate, bits):
    import numpy as np
    data = np.clip(data, -1.0, 1.0)
    if bits == 24:
        iv = np.round(data * 8388607).astype("<i4")
        b = iv.view(np.uint8).reshape(-1, 4)[:, :3].tobytes()
    else:
        b = np.round(data * 32767).astype("<i2").tobytes()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(data.shape[1])
        w.setsampwidth(bits // 8)
        w.setframerate(rate)
        w.writeframes(b)


def read_channels(path, rate=48000):
    import numpy as np
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=channels",
                        "-of", "csv=p=0", path], capture_output=True, text=True)
    ch = int(r.stdout.strip() or 1)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-ar", str(rate), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32).reshape(-1, ch), rate


def read_mono(path, rate=48000, af=None):
    import numpy as np
    cmd = ["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(rate)]
    if af:
        cmd += ["-af", af]
    raw = subprocess.run(cmd + ["-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32), rate


async def audio_render(a):
    import numpy as np
    async with pw()() as p:
        b = await p.chromium.launch(args=CHROME_ARGS)
        pg = await b.new_page()
        pg.on("pageerror", lambda e: print(f"[page error] {e}", file=sys.stderr))
        await pg.goto(Path(a.html).resolve().as_uri())
        r = await pg.evaluate(AUDIO_JS, {"duration": a.duration, "rate": a.rate})
        await b.close()
    chans = [np.frombuffer(base64.b64decode(c), np.float32) for c in r["ch"]]
    data = np.stack(chans, axis=1)
    write_wav(a.out, data, r["rate"], a.bits)
    peak = float(np.max(np.abs(data))) if data.size else 0.0
    print(f"Wrote {a.out}: {data.shape[0]/r['rate']:.3f}s, {data.shape[1]}ch, {r['rate']}Hz, {a.bits}-bit, "
          f"peak {20*math.log10(max(peak,1e-9)):.1f} dBFS" + ("  (CLIPPED: lower master gain)" if peak > 1 else ""))


def audio_peak(a):
    import numpy as np
    xc, sr = read_channels(a.file)
    x = xc[np.arange(len(xc)), np.argmax(np.abs(xc), axis=1)] if len(xc) else xc[:, 0]  # loudest channel per sample
    if not len(x):
        die("empty audio")
    i = int(np.argmax(np.abs(x)))
    hop = int(sr * 0.002)
    env = np.sqrt(np.convolve(x * x, np.ones(hop) / hop, "same"))
    pk_env = env[i]
    j = i
    while j > 0 and env[j] > 0.1 * pk_env and i - j < sr * 0.25:
        j -= 1
    print(f"peak: t={i/sr:.4f}s  ({20*math.log10(abs(x[i])+1e-12):.1f} dBFS)  onset ~{j/sr:.4f}s  "
          f"length {len(x)/sr:.3f}s")
    if a.align is not None:
        off = a.align - i / sr
        print(f"to land this peak at {a.align}s place the file at {off:.4f}s  (mux --sfx {a.file}@{off:.4f})")


def audio_drop(a):
    import numpy as np
    x, sr = read_mono(a.file, 8000, "lowpass=f=150,lowpass=f=150")
    win = int(sr * 0.05)
    n = len(x) // win
    rms = np.sqrt(np.mean(x[:n * win].reshape(n, win) ** 2, axis=1) + 1e-12)
    db = 20 * np.log10(rms)
    k = int(1.0 / 0.05)
    scores = []
    for i in range(k, n - k):
        scores.append((float(np.mean(db[i:i + k]) - np.mean(db[i - k:i])), i * 0.05))
    if not scores:
        die("audio too short for drop detection (needs > 2s)")
    scores.sort(reverse=True)
    picked = []
    for s, t in scores:
        if all(abs(t - u) > 2.0 for _, u in picked):
            picked.append((s, t))
        if len(picked) == 3:
            break
    print(f"drop: biggest low-band (<150 Hz) energy jump at t={picked[0][1]:.2f}s (+{picked[0][0]:.1f} dB, 1s vs 1s)")
    for s, t in picked[1:]:
        print(f"      runner-up t={t:.2f}s (+{s:.1f} dB)")


def _ln_measure(inp):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", inp, "-af",
                        "loudnorm=I=-14:TP=-1:LRA=11:print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr, re.S)
    if not m:
        die("loudness measurement failed:\n" + r.stderr[-800:])
    j = json.loads(m.group(0))
    return float(j["input_i"]), float(j["input_tp"])


def loudnorm(inp, out, lufs=-14.0, tp=-1.0, lra=11.0, rate=None):
    """Two-pass loudness normalisation. Pass 1 measures integrated loudness + true peak.
    If a plain gain reaches the target without exceeding the true-peak ceiling, apply exactly
    that gain (transparent). Otherwise apply gain into a peak limiter and re-measure, correcting
    the gain until integrated loudness is within 0.1 LU (ffmpeg's dynamic loudnorm mode is
    avoided: it misses the target on short clips and pumps)."""
    if rate is None:
        rr = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                             "stream=sample_rate", "-of", "csv=p=0", inp], capture_output=True, text=True)
        rate = int(rr.stdout.strip() or 48000)
    codec = ["-c:a", "pcm_s24le"] if out.endswith(".wav") else ["-c:a", "aac", "-b:a", "192k"]
    i_in, tp_in = _ln_measure(inp)
    gain = lufs - i_in
    if tp_in + gain <= tp:
        kind = "linear gain"
        af = f"volume={gain:.3f}dB"
    else:
        kind = "gain + limiter"
        lim = 10 ** ((tp - 0.5) / 20)
        tmp = out + ".tmp.wav"
        for _ in range(8):
            af = (f"volume={gain:.3f}dB,aresample={rate*4},alimiter=limit={lim:.4f}:level=false:attack=1:release=60,"
                  f"aresample={rate}")
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", inp, "-af", af, "-c:a", "pcm_f32le", tmp],
                           check=True)
            i_now, _ = _ln_measure(tmp)
            if abs(i_now - lufs) <= 0.05:
                break
            gain += lufs - i_now
        os.remove(tmp)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", inp, "-af", af, "-ar", str(rate)] + codec + [out],
                   check=True)
    i_out, tp_out = _ln_measure(out)
    return i_in, i_out, f"{kind}, {gain:+.2f} dB, true peak {tp_out:.1f} dBTP"


def measure_lufs(path):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-map", "0:a:0", "-af", "ebur128=peak=true",
                        "-f", "null", "-"], capture_output=True, text=True)
    m = re.findall(r"I:\s+(-?[\d.]+) LUFS", r.stderr)
    return float(m[-1]) if m else float("nan")


def audio_loudnorm(a):
    i_in, i_out, kind = loudnorm(a.inp, a.out, a.lufs, a.tp)
    print(f"Wrote {a.out}: {i_in:.1f} LUFS -> {i_out:.1f} LUFS (target {a.lufs}, ceiling {a.tp} dBTP; {kind}); "
          f"ebur128 check {measure_lufs(a.out):.1f} LUFS")


def media_duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                       capture_output=True, text=True)
    return float(r.stdout.strip())


def parse_sfx(s):
    m = re.match(r"^(.*)@(-?[\d.]+)(?::([\d.]+))?$", s)
    if not m:
        die(f"--sfx must be file@time[:gain], got {s!r}")
    return m.group(1), float(m.group(2)), float(m.group(3) or 1.0)


def mux(a):
    need_ffmpeg()
    vdur = media_duration(a.video)
    sfx = [parse_sfx(s) for s in (a.sfx or [])]
    tmp = Path(tempfile.mkdtemp(prefix="fm-mux-"))
    try:
        mix = tmp / "mix.wav"
        cmd = ["ffmpeg", "-y", "-loglevel", "error"]
        if a.offset < 0:
            cmd += ["-ss", str(-a.offset)]
        cmd += ["-i", a.audio]
        for f, _, _ in sfx:
            cmd += ["-i", f]
        chain = ["aresample=48000", "aformat=channel_layouts=stereo"]
        if a.offset > 0:
            chain.append(f"adelay={int(round(a.offset*1000))}:all=1")
        if a.volume != 1.0:
            chain.append(f"volume={a.volume}")
        fc = [f"[0:a]{','.join(chain)}[m0]"]
        for k, (f, t, g) in enumerate(sfx, start=1):
            fc.append(f"[{k}:a]aresample=48000,aformat=channel_layouts=stereo,"
                      f"adelay={max(0,int(round(t*1000)))}:all=1,volume={g}[m{k}]")
        if sfx:
            fc.append("".join(f"[m{k}]" for k in range(len(sfx) + 1)) +
                      f"amix=inputs={len(sfx)+1}:normalize=0:duration=longest[mx]")
            last = "mx"
        else:
            last = "m0"
        fc.append(f"[{last}]apad,atrim=0:{vdur:.6f}[out]")
        cmd += ["-filter_complex", ";".join(fc), "-map", "[out]", "-c:a", "pcm_f32le", str(mix)]
        subprocess.run(cmd, check=True)
        ceiling = a.tp
        for attempt in range(3):
            src = mix
            if a.lufs is not None:
                normed = tmp / "norm.wav"
                i_in, i_out, kind = loudnorm(str(mix), str(normed), a.lufs, ceiling, rate=48000)
                print(f"loudnorm: {i_in:.1f} -> {i_out:.1f} LUFS ({kind})")
                src = normed
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", a.video, "-i", str(src),
                            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", a.bitrate,
                            "-shortest", "-movflags", "+faststart", a.out], check=True)
            if a.lufs is None:
                break
            _, tp_enc = _ln_measure(a.out)  # AAC can overshoot the WAV's true peak: re-limit if it does
            if tp_enc <= a.tp + 0.2:
                break
            print(f"  AAC true peak {tp_enc:.1f} dBTP > {a.tp}: tightening limiter")
            ceiling -= (tp_enc - a.tp) + 0.3
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    extra = f", {len(sfx)} sfx" if sfx else ""
    lu = ""
    if a.lufs is not None:
        i_f, tp_f = _ln_measure(a.out)
        lu = f", {i_f:.1f} LUFS integrated, true peak {tp_f:.1f} dBTP"
    print(f"Wrote {a.out} ({vdur:.3f}s{extra}{lu})")


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description="frame-motion renderer (see module docstring). Default size 1920x1080 (16:9).",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def scene(sp, html=True):
        if html:
            sp.add_argument("html")
        sp.add_argument("--preset", choices=list(PRESETS),
                        help="16:9=1920x1080 (default), 9:16=1080x1920, 4:5=1080x1350, 1:1=1080x1080")
        sp.add_argument("--width", type=int, help="override width (default from --preset, 1920)")
        sp.add_argument("--height", type=int, help="override height (default from --preset, 1080)")
        sp.add_argument("--device-scale", type=float, default=1.0)
        sp.add_argument("--webgl", action="store_true", help="SwiftShader WebGL so 3D isn't black headless")
        sp.add_argument("--no-harden", action="store_true", help="don't freeze CSS/Web animations")

    def video(sp, crf=16, fps=30):
        sp.add_argument("out")
        sp.add_argument("--fps", type=int, default=fps)
        sp.add_argument("--duration", type=float, required=True)
        sp.add_argument("--start", type=float, default=0.0, help="chunk start time (s)")
        sp.add_argument("--jpeg", action="store_true", help="JPEG captures (faster drafts)")
        sp.add_argument("--quality", type=int, default=92)
        sp.add_argument("--crf", type=int, default=crf)
        sp.add_argument("--x264-preset", default="medium")
        sp.add_argument("--x264-threads", type=int, default=0, help="0 = auto (parallel sets cpu/jobs)")
        sp.add_argument("--sub", type=int, default=1, help="motion-blur subframes per frame (1 = off)")
        sp.add_argument("--shutter", type=float, default=0.5, help="fraction of frame interval (0.5 = 180deg)")
        sp.add_argument("--cuts", default="", help="hard-cut times t1,t2: blur never crosses them")
        sp.add_argument("--align", choices=["start", "center"], default="start",
                        help="subframes start at the frame time (default) or are centred on it")

    p = sub.add_parser("probe", help="stills at given times")
    scene(p); p.add_argument("times", nargs="+", type=float); p.add_argument("--out", default="probe")

    p = sub.add_parser("poster", help="single still / thumbnail")
    scene(p); p.add_argument("out"); p.add_argument("--t", type=float, default=0.0)

    p = sub.add_parser("sheet", help="contact sheet from a scene or a video")
    scene(p, html=False); p.add_argument("src")
    p.add_argument("--times"); p.add_argument("--every", type=float); p.add_argument("--duration", type=float)
    p.add_argument("--cols", type=int, default=4); p.add_argument("--thumb", type=int, default=480)
    p.add_argument("--out", default="sheet.png")

    p = sub.add_parser("beats", help="one labelled frame per beat")
    scene(p); p.add_argument("--bpm", type=float, required=True); p.add_argument("--duration", type=float, required=True)
    p.add_argument("--offset", type=float, default=0.0); p.add_argument("--per-bar", type=int, default=4)
    p.add_argument("--cols", type=int, default=0); p.add_argument("--thumb", type=int, default=384)
    p.add_argument("--out", default="beats")

    p = sub.add_parser("full", help="render MP4 (optional motion blur)")
    scene(p); video(p)
    p.add_argument("--frames", type=int, default=0, help=argparse.SUPPRESS)
    p.add_argument("--total", type=float, default=0.0, help=argparse.SUPPRESS)

    p = sub.add_parser("draft", help="fast half-res 30fps JPEG preview")
    scene(p); video(p, crf=0, fps=30)

    p = sub.add_parser("parallel", help="full render split over N browser processes")
    scene(p); video(p); p.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    p.add_argument("--keep-chunks", action="store_true")

    p = sub.add_parser("pops", help="detect frame-diff spikes / single-frame flashes")
    scene(p, html=False); p.add_argument("src")
    p.add_argument("--threshold", type=float, default=3.0); p.add_argument("--window", type=int, default=3)
    p.add_argument("--floor", type=float, default=2.0, help="ignore diffs below this (0-255 scale)")
    p.add_argument("--min-median", type=float, default=0.25)
    p.add_argument("--cuts", default="", help="intentional cut times to ignore")
    p.add_argument("--duration", type=float); p.add_argument("--fps", type=int, default=0)
    p.add_argument("--bpm", type=float, help="tag each spike as on-beat / OFF-BEAT")
    p.add_argument("--offset", type=float, default=0.0); p.add_argument("--beat-tol", type=float, default=4.0,
                   help="frames from a beat that still count as on-beat")

    p = sub.add_parser("loop", help="loop seam check (position + velocity)")
    scene(p); p.add_argument("--duration", type=float, required=True); p.add_argument("--fps", type=int, default=30)
    p.add_argument("--tol", type=float, default=0.5); p.add_argument("--vel-ratio", type=float, default=1.5)

    p = sub.add_parser("determinism", help="seek a, b, a and compare")
    scene(p); p.add_argument("--times", required=True)

    pa = sub.add_parser("audio", help="audio tools").add_subparsers(dest="acmd", required=True)
    p = pa.add_parser("render"); p.add_argument("html"); p.add_argument("out")
    p.add_argument("--duration", type=float, required=True); p.add_argument("--rate", type=int, default=48000)
    p.add_argument("--bits", type=int, choices=[16, 24], default=24)
    p = pa.add_parser("peak"); p.add_argument("file"); p.add_argument("--align", type=float)
    p = pa.add_parser("drop"); p.add_argument("file")
    p = pa.add_parser("loudnorm"); p.add_argument("inp"); p.add_argument("out")
    p.add_argument("--lufs", type=float, default=-14.0); p.add_argument("--tp", type=float, default=-1.0)

    p = sub.add_parser("mux", help="add audio (+ sfx, loudnorm) to a rendered video")
    p.add_argument("video"); p.add_argument("audio"); p.add_argument("out")
    p.add_argument("--offset", type=float, default=0.0); p.add_argument("--volume", type=float, default=1.0)
    p.add_argument("--sfx", action="append", help="file@time[:gain], repeatable")
    p.add_argument("--lufs", type=float); p.add_argument("--tp", type=float, default=-1.0)
    p.add_argument("--bitrate", default="192k")

    a = ap.parse_args()
    if hasattr(a, "preset"):
        resolve_size(a)
    if a.cmd == "mux":
        return mux(a)
    if a.cmd == "parallel":
        return parallel(a)
    if a.cmd == "audio":
        need_ffmpeg()
        if a.acmd == "render":
            return asyncio.run(audio_render(a))
        return {"peak": audio_peak, "drop": audio_drop, "loudnorm": audio_loudnorm}[a.acmd](a)
    fn = {"probe": probe, "poster": poster, "sheet": sheet_cmd, "beats": beats, "full": full, "draft": draft,
          "pops": pops, "loop": loop_cmd, "determinism": determinism}[a.cmd]
    asyncio.run(fn(a))


if __name__ == "__main__":
    main()
