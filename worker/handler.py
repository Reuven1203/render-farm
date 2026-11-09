"""
Render farm: the Runpod serverless worker.

Every job is one of:
  {"op": "ping"}                                       -> GPU / Blender / ffmpeg check
  {"op": "render", "job": ID, "start": S, "end": E}   -> render frames S..E of the job's scene
  {"op": "pull", "job": ID, "slot": N, "start": S, "end": E, "sub": 2, "ordered": false}
                                                       -> one long Blender session that keeps claiming the next
                                                          small batch of frames until none are left
  {"op": "stitch", "job": ID, "start": S, "end": E,   -> frames -> MP4 (+ loop-seam check)
   "fps": 30, "loop": true, "name": "clip", "crf": 16, "keep_frames": false}

Files live on the shared network volume (mounted at /runpod-volume):
  jobs/<ID>/scene.blend        uploaded by farm.py
  jobs/<ID>/frames/f_0001.png  written by render jobs
  jobs/<ID>/claims/0001-0002   one file per batch a GPU has taken (heartbeat = its modified time)
  jobs/<ID>/workers/w<N>.json  which batch each GPU is on right now
  jobs/<ID>/<name>.mp4         written by the stitch job, downloaded by farm.py
"""
import collections
import glob
import json
import os
import re
import shutil
import subprocess
import threading
import time

import runpod

VOL = "/runpod-volume"
BLENDER = os.environ.get("BLENDER_BIN", "/opt/blender-5.2.0-linux-x64/blender")
JOB_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")
HANG_WARMING = 900     # seconds of silence from Blender before it counts as hung, while it is still warming up
HANG_AFTER_WARM = 600  # ... and once it is rendering

# Applied inside Blender before rendering: PNG output, overwrite on retry,
# and GPU devices switched on when the scene uses Cycles (EEVEE uses the GPU already).
RENDER_SETUP_BASE = r"""
import bpy
s = bpy.context.scene
r = s.render
r.use_overwrite = True
r.use_placeholder = False
try:
    r.image_settings.media_type = 'IMAGE'   # Blender 5: a scene saved as video output has to switch to stills first
except Exception:
    pass
for _attr, _val in (('file_format', 'PNG'), ('color_depth', '8')):
    try:
        setattr(r.image_settings, _attr, _val)
    except Exception as _e:
        print('FARM warning: could not set %s=%s (%s)' % (_attr, _val, _e))
if r.engine == 'CYCLES':
    prefs = bpy.context.preferences.addons['cycles'].preferences
    for backend in ('OPTIX', 'CUDA'):
        try:
            prefs.compute_device_type = backend
            prefs.get_devices()
            for d in prefs.devices:
                d.use = d.type == backend
            break
        except Exception:
            pass
    s.cycles.device = 'GPU'
"""

RENDER_SETUP = RENDER_SETUP_BASE + r"""
import json, os
_ctl = os.environ.get('FARM_CTL')

def _farm_limit(scene, *args):
    # the orchestrator can hand the tail of this batch to idle GPUs by writing a lower end frame here
    if not _ctl:
        return
    try:
        with open(_ctl) as f:
            end = int(json.load(f)['end'])
    except Exception:
        return
    if scene.frame_current >= end:
        print('FARM stopped after frame %d: the rest was handed to other GPUs' % scene.frame_current, flush=True)
        os._exit(0)

bpy.app.handlers.render_write.append(_farm_limit)
print('FARM engine=%s res=%dx%d@%d%%' % (r.engine, r.resolution_x, r.resolution_y, r.resolution_percentage))
"""


def gpu_name():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=20).stdout.strip()
        return out.splitlines()[0] if out else "unknown"
    except Exception:
        return "unknown"


def job_dir(job):
    if not isinstance(job, str) or not JOB_RE.match(job):
        raise ValueError("bad job id")
    d = os.path.join(VOL, "jobs", job)
    if not os.path.isdir(d):
        raise FileNotFoundError(f"job folder not found on the volume: jobs/{job}")
    return d


def tail(text, n=25):
    return "\n".join(text.strip().splitlines()[-n:])


def stream_blender(cmd, logpath, header, env=None, on_line=None, append=False, on_start=None):
    """Run Blender, mirroring its output to logpath. Returns (last 400 lines, exit code)."""
    keep = collections.deque(maxlen=400)
    last_progress = 0.0
    with open(logpath, "a" if append else "w", buffering=1) as lf:
        lf.write(f"[{header}]\n")
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env=env)
        if on_start:
            on_start(p)
        for raw in p.stdout:
            line = raw.rstrip()
            keep.append(line)
            if on_line:
                on_line(line)
            if "| Fra:" in line or line.startswith("Fra:"):  # Cycles prints dozens of these per frame
                now = time.time()
                if now - last_progress < 1.5:
                    continue
                last_progress = now
            lf.write(line[:300] + "\n")
        code = p.wait()
        lf.write(f"[blender exited with code {code}]\n")
    return list(keep), code


def op_ping(_inp):
    ver = subprocess.run([BLENDER, "--version"], capture_output=True, text=True, timeout=120).stdout
    du = shutil.disk_usage(VOL)
    return {
        "gpu": gpu_name(),
        "blender": ver.splitlines()[0] if ver else "missing",
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "volume_free_gb": round(du.free / 1e9, 1),
    }


def op_render(inp):
    d = job_dir(inp.get("job"))
    s, e = int(inp["start"]), int(inp["end"])
    if s > e:
        raise ValueError("start must be <= end")
    blend = os.path.join(d, "scene.blend")
    frames = os.path.join(d, "frames")
    os.makedirs(frames, exist_ok=True)

    # render on local disk, then move finished PNGs to the volume, so a crash
    # never leaves a half-written frame behind
    tmp = f"/tmp/render_{inp['job']}_{s}_{e}"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    local = os.path.join(tmp, "scene.blend")
    shutil.copy(blend, local)
    setup = os.path.join(tmp, "setup.py")
    with open(setup, "w") as f:
        f.write(RENDER_SETUP)

    cmd = [BLENDER, "-b", local, "--python", setup,
           "-o", os.path.join(tmp, "f_####"), "-F", "PNG", "-x", "1",
           "-s", str(s), "-e", str(e), "-a"]
    t0 = time.time()
    logs = os.path.join(d, "logs")
    ctl_dir = os.path.join(d, "ctl")
    os.makedirs(logs, exist_ok=True)
    os.makedirs(ctl_dir, exist_ok=True)
    key = str(inp.get("key") or f"{s}-{e}")
    if not re.match(r"^\d+-\d+$", key):
        raise ValueError("bad batch key")
    ctl = os.path.join(ctl_dir, f"{key}.json")  # may already exist: the orchestrator can shorten a batch before it starts
    # live console: lines are appended to the volume so the UI can read them while Blender runs
    out_lines, returncode = stream_blender(
        cmd, os.path.join(logs, f"{key}.log"),
        f"worker {os.environ.get('RUNPOD_POD_ID', '?')} · {gpu_name()} · frames {s}-{e}",
        env=dict(os.environ, FARM_CTL=ctl))
    secs = time.time() - t0

    produced = sorted(glob.glob(os.path.join(tmp, "f_*.png")))
    want = e - s + 1
    try:
        with open(ctl) as f:
            want = min(e, int(json.load(f)["end"])) - s + 1  # the batch was shortened: the rest was rendered by other GPUs
    except Exception:
        pass
    if returncode != 0 or len(produced) < want:
        shutil.rmtree(tmp, ignore_errors=True)
        return {"error": f"Blender rendered {len(produced)}/{want} frames (exit {returncode})\n"
                         + tail("\n".join(out_lines))}
    for f in produced:
        shutil.move(f, os.path.join(frames, os.path.basename(f)))
    shutil.rmtree(tmp, ignore_errors=True)

    info = [l for l in out_lines if l.startswith("FARM ")]
    return {
        "frames": want,
        "seconds": round(secs, 1),
        "sec_per_frame": round(secs / want, 2),
        "gpu": gpu_name(),
        "scene": info[0][5:] if info else "",
    }


# Runs inside Blender for the whole life of a "pull" job: the scene stays loaded while this GPU keeps taking the
# next unclaimed batch of frames, so there is no Blender start-up between batches and no GPU waits for a "tail".
PULL_LOOP = r"""
import json, os, shutil, time, traceback
cfg = json.loads(os.environ['FARM_PULL'])
S, E, SUB, ordered = cfg['start'], cfg['end'], cfg['sub'], cfg['ordered']
FR, CL, TMP, SLOT = cfg['frames'], cfg['claims'], cfg['tmp'], cfg['slot']
sc = bpy.context.scene
batches = [(a, min(a + SUB - 1, E)) for a in range(S, E + 1, SUB)]

def have(f):
    return os.path.exists(os.path.join(FR, 'f_%04d.png' % f))

def claim(name):
    # O_EXCL makes the claim atomic: when two GPUs reach the same batch, exactly one gets it
    try:
        fd = os.open(os.path.join(CL, name), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    os.write(fd, json.dumps({'slot': SLOT, 't': time.time()}).encode())
    os.close(fd)
    return True

def draw(f):
    t = time.time()
    sc.frame_set(f)
    sc.render.filepath = os.path.join(TMP, 'f_%04d' % f)
    bpy.ops.render.render(write_still=True)
    src = os.path.join(TMP, 'f_%04d.png' % f)
    dst = os.path.join(FR, 'f_%04d.png' % f)
    shutil.copyfile(src, dst + '.part')
    os.replace(dst + '.part', dst)   # a frame is only visible once it is complete
    os.remove(src)
    print('FARMFRAME %d %.2f' % (f, time.time() - t), flush=True)

def warm_up():
    # a cold GPU spends minutes compiling its render kernels on the first frame. Do that on a throwaway
    # thumbnail BEFORE claiming a batch, so a slow starter never holds frames the other GPUs could be drawing.
    t = time.time()
    pct = sc.render.resolution_percentage
    cy = sc.render.engine == 'CYCLES'
    samples = sc.cycles.samples if cy else None
    try:
        sc.render.resolution_percentage = 10
        if cy:
            sc.cycles.samples = 1
        sc.frame_set(S)
        bpy.ops.render.render(write_still=False)
    except Exception:
        traceback.print_exc()
    finally:
        sc.render.resolution_percentage = pct
        if cy:
            sc.cycles.samples = samples
    print('FARMWARM %.1f' % (time.time() - t), flush=True)

try:
    warm_up()
    while True:
        took = False
        for a, b in batches:
            name = '%04d-%04d' % (a, b)
            if not ordered and all(have(f) for f in range(a, b + 1)):
                continue
            if not claim(name) and not ordered:
                continue
            took = True
            print('FARMCLAIM ' + name, flush=True)
            for f in range(a, b + 1):
                if ordered:
                    sc.frame_set(f)          # a simulation has to be stepped through every frame, rendered or not
                if not have(f):
                    draw(f)
        if ordered or not took:
            break
    print('FARMPULL done', flush=True)
except Exception:
    traceback.print_exc()
    print('FARMPULL failed', flush=True)
"""


def op_pull(inp):
    d = job_dir(inp.get("job"))
    s, e = int(inp["start"]), int(inp["end"])
    slot, sub, ordered = int(inp.get("slot", 0)), max(1, int(inp.get("sub", 2))), bool(inp.get("ordered"))
    if s > e:
        raise ValueError("start must be <= end")
    frames, claims, workers, logs = (os.path.join(d, x) for x in ("frames", "claims", "workers", "logs"))
    for p in (frames, claims, workers, logs):
        os.makedirs(p, exist_ok=True)

    tmp = f"/tmp/pull_{inp['job']}_{slot}"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    local = os.path.join(tmp, "scene.blend")
    shutil.copy(os.path.join(d, "scene.blend"), local)
    setup = os.path.join(tmp, "setup.py")
    with open(setup, "w") as f:
        f.write(RENDER_SETUP_BASE + PULL_LOOP)

    status_path = os.path.join(workers, f"w{slot}.json")
    state = {"batch": None, "frames": 0, "warm": False, "hung": False, "last": time.time(), "proc": None}
    stop = threading.Event()

    def publish():
        with open(status_path + ".tmp", "w") as f:
            json.dump({"batch": state["batch"], "t": time.time()}, f)
        os.replace(status_path + ".tmp", status_path)

    def beat():
        # the orchestrator treats a claim nobody touches for a couple of minutes as abandoned (crashed GPU)
        while not stop.wait(15):
            # Blender prints constantly while it renders; silence for this long means it is hung, not slow
            limit = HANG_AFTER_WARM if state["warm"] else HANG_WARMING
            if state["proc"] and time.time() - state["last"] > limit:
                state["hung"] = True
                state["proc"].kill()
            try:
                if state["batch"]:
                    os.utime(os.path.join(claims, state["batch"]))
                publish()
            except OSError:
                pass

    def on_line(line):
        state["last"] = time.time()
        if line.startswith("FARMWARM"):
            state["warm"] = True
        elif line.startswith("FARMCLAIM "):
            state["batch"] = line.split()[1]
            try:
                publish()
            except OSError:
                pass
        elif line.startswith("FARMFRAME "):
            state["frames"] += 1

    cmd = [BLENDER, "-b", local, "--python", setup]
    env = dict(os.environ, FARM_PULL=json.dumps({
        "start": s, "end": e, "sub": sub, "ordered": ordered, "slot": slot,
        "frames": frames, "claims": claims, "tmp": tmp}))
    t0 = time.time()
    publish()
    threading.Thread(target=beat, daemon=True).start()
    try:
        out_lines, code = stream_blender(
            cmd, os.path.join(logs, f"w{slot}.log"),
            f"worker {os.environ.get('RUNPOD_POD_ID', '?')} · {gpu_name()} · GPU {slot + 1}", env=env, on_line=on_line,
            append=True, on_start=lambda p: state.update(proc=p, last=time.time()))
    finally:
        stop.set()
    secs = time.time() - t0
    shutil.rmtree(tmp, ignore_errors=True)
    finished = any(l.startswith("FARMPULL done") for l in out_lines)
    if not finished or code != 0:
        # give the batch back so another GPU can take it straight away
        if state["batch"]:
            try:
                os.remove(os.path.join(claims, state["batch"]))
            except OSError:
                pass
        why = "Blender stopped responding and was restarted" if state["hung"] else f"Blender stopped (exit {code})"
        return {"error": f"{why} after {state['frames']} frame(s)\n" + tail("\n".join(out_lines))}
    try:
        os.remove(status_path)
    except OSError:
        pass
    return {"frames": state["frames"], "seconds": round(secs, 1),
            "sec_per_frame": round(secs / state["frames"], 2) if state["frames"] else 0, "gpu": gpu_name()}


def seam_psnr(a, b):
    """PSNR between the first frame and the loop-end frame. 'inf' = identical."""
    p = subprocess.run(["ffmpeg", "-v", "info", "-i", a, "-i", b, "-lavfi", "psnr", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.search(r"average:(\S+)", p.stderr)
    return m.group(1) if m else "n/a"


def op_stitch(inp):
    d = job_dir(inp.get("job"))
    s, e = int(inp["start"]), int(inp["end"])
    fps = float(inp.get("fps", 30))
    loop = bool(inp.get("loop", True))
    crf = int(inp.get("crf", 16))
    name = NAME_RE.sub("_", str(inp.get("name", "render")))[:120] or "render"
    frames = os.path.join(d, "frames")

    missing = [i for i in range(s, e + 1) if not os.path.exists(os.path.join(frames, f"f_{i:04d}.png"))]
    if missing:
        return {"error": f"{len(missing)} frames missing, e.g. {missing[:20]}"}

    # a seamless loop's last frame repeats its first, so the video stops one frame early
    last = e - 1 if loop and e > s else e
    count = last - s + 1
    out = os.path.join(d, f"{name}.mp4")
    p = subprocess.run(["ffmpeg", "-y", "-v", "error", "-framerate", str(fps), "-start_number", str(s),
                        "-i", os.path.join(frames, "f_%04d.png"), "-frames:v", str(count),
                        "-c:v", "libx264", "-preset", "slow", "-crf", str(crf), "-pix_fmt", "yuv420p",
                        "-movflags", "+faststart", out], capture_output=True, text=True)
    if p.returncode != 0 or not os.path.exists(out):
        return {"error": "ffmpeg failed\n" + tail(p.stderr)}

    seam = None
    if loop and e > s:
        psnr = seam_psnr(os.path.join(frames, f"f_{s:04d}.png"), os.path.join(frames, f"f_{e:04d}.png"))
        try:
            ok = psnr == "inf" or float(psnr) >= 45
        except ValueError:
            ok = False
        seam = {"psnr_db": psnr, "pass": ok}

    if not inp.get("keep_frames", False):
        shutil.rmtree(frames, ignore_errors=True)
        for extra in ("claims", "workers", "ctl"):
            shutil.rmtree(os.path.join(d, extra), ignore_errors=True)
        try:
            os.remove(os.path.join(d, "scene.blend"))
        except OSError:
            pass

    return {"video": f"jobs/{inp['job']}/{name}.mp4", "bytes": os.path.getsize(out),
            "frames": count, "fps": fps, "seam": seam}


OPS = {"ping": op_ping, "render": op_render, "pull": op_pull, "stitch": op_stitch}


def handler(job):
    inp = job.get("input") or {}
    op = OPS.get(inp.get("op"))
    if not op:
        return {"error": f"unknown op {inp.get('op')!r}; use one of {sorted(OPS)}"}
    try:
        return op(inp)
    except Exception as ex:  # surfaced as a FAILED job with the reason
        return {"error": f"{type(ex).__name__}: {ex}"}


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
