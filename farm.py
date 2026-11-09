#!/usr/bin/env python3
"""
Render farm: render a .blend on several Runpod GPUs at once.

    python3 farm.py setup                         # one time: save your Runpod keys
    python3 farm.py test                          # check the GPU workers are working
    python3 farm.py render "My Loop.blend"        # render on 4 GPUs -> MP4 next to the file
    python3 farm.py render "My Loop.blend" --workers 6 --no-loop

What `render` does:
  1. Opens the file in your local Blender, drops images whose files are missing,
     packs everything else into the .blend, and reads the frame range / fps.
  2. Uploads it to the farm's shared Runpod storage.
  3. Puts the frames in one shared pile of small batches. Each of the --workers GPUs
     keeps one Blender session open and takes the next batch whenever it is free, so
     every GPU works until the last frame. A scene with unbaked simulations has to be
     drawn in order, so it uses one GPU instead. A GPU that crashes is replaced.
  4. Joins the frames into an MP4 (dropping the duplicate last frame of a seamless
     loop), checks the loop seam, downloads the MP4 and cleans up the storage.

Only Python's standard library is used, so it runs with the Mac's built-in python3
or with Blender's own Python.
"""
import argparse
import atexit
import datetime
import getpass
import hashlib
import hmac
import json
import math
import os
import re
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.expanduser("~/.bassmint_farm.json")
DEFAULTS = {
    "endpoint_id": "a3u5mshjs6dp6v",  # bassmint-render-farm serverless endpoint
    "volume_id": "dqrdx4v647",      # bassmint-render-farm network volume
    "datacenter": "US-IL-1",
    "price_per_gpu_hour": 1.10,     # RTX 4090 serverless rate (USD)
    "default_workers": 4,
}
PART_SIZE = 64 * 1024 * 1024
# Runpod sits behind Cloudflare, which blocks Python's default "Python-urllib" agent (error 1010)
USER_AGENT = "aws-cli/2.17.0 md/render-farm#1.1"
MAX_RETRIES = 2
PULL_RETRIES = 5    # a replaced GPU costs almost nothing (finished frames are kept), so be generous before giving up
CLAIM_STALE = 150   # seconds without a heartbeat before a claim counts as abandoned by a crashed GPU


# --------------------------------------------------------------------------- utils
def say(msg=""):
    print(msg, flush=True)


def die(msg):
    say(f"\n✗ {msg}")
    sys.exit(1)


def ssl_context():
    try:
        import certifi  # present in Blender's Python and many installs
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


SSL = ssl_context()


def fmt_time(s):
    s = int(max(0, s))
    return f"{s // 60}:{s % 60:02d}"


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    return cfg


def need_keys(cfg):
    missing = [k for k in ("api_key", "s3_access_key", "s3_secret_key", "endpoint_id") if not cfg.get(k)]
    if missing:
        die(f"missing {', '.join(missing)} — run:  python3 farm.py setup")


# --------------------------------------------------------------------------- S3 (Runpod network volume)
class Volume:
    """Minimal S3 client (SigV4, path-style) for a Runpod network volume."""

    def __init__(self, cfg):
        self.bucket = cfg["volume_id"]
        self.region = cfg["datacenter"]
        self.host = f"s3api-{cfg['datacenter'].lower()}.runpod.io"
        self.ak = cfg["s3_access_key"]
        self.sk = cfg["s3_secret_key"]

    def _request(self, method, key, query=None, body=b"", headers=None, stream=False):
        query = query or {}
        headers = dict(headers or {})
        now = datetime.datetime.now(datetime.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        day = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(body).hexdigest()
        path = "/" + self.bucket + ("/" + urllib.parse.quote(key, safe="/-_.~") if key else "")
        q = "&".join(f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(str(v), safe='-_.~')}"
                     for k, v in sorted(query.items()))
        headers.update({"host": self.host, "x-amz-date": amz_date, "x-amz-content-sha256": payload_hash})
        lower = {k.lower(): str(v).strip() for k, v in headers.items()}
        signed = ";".join(sorted(lower))
        canon_headers = "".join(f"{k}:{lower[k]}\n" for k in sorted(lower))
        canon = "\n".join([method, path, q, canon_headers, signed, payload_hash])
        scope = f"{day}/{self.region}/s3/aws4_request"
        to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canon.encode()).hexdigest()])

        def h(k, m):
            return hmac.new(k, m.encode(), hashlib.sha256).digest()

        key_ = h(h(h(h(("AWS4" + self.sk).encode(), day), self.region), "s3"), "aws4_request")
        sig = hmac.new(key_, to_sign.encode(), hashlib.sha256).hexdigest()
        lower["authorization"] = (f"AWS4-HMAC-SHA256 Credential={self.ak}/{scope}, "
                                  f"SignedHeaders={signed}, Signature={sig}")
        lower.pop("host")
        lower["user-agent"] = USER_AGENT
        url = f"https://{self.host}{path}" + (f"?{q}" if q else "")
        req = urllib.request.Request(url, data=body if method in ("PUT", "POST") else None,
                                     method=method, headers=lower)
        for attempt in range(5):
            try:
                resp = urllib.request.urlopen(req, context=SSL, timeout=300)
                return resp if stream else (resp.status, dict(resp.headers), resp.read())
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:400]
                if e.code in (500, 502, 503, 504) and attempt < 4:
                    time.sleep(2 * (attempt + 1))
                    continue
                if e.code in (401, 403):
                    if "error code: 10" in detail:
                        die(f"Cloudflare blocked the storage request ({detail.strip()}). This is a network block, "
                            "not your keys — tell Claude the error code.")
                    die(f"storage refused the request ({e.code}). Check the S3 keys in {CONFIG_PATH}.\n{detail}")
                raise RuntimeError(f"S3 {method} {key}: HTTP {e.code} {detail}")
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt < 4:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise RuntimeError(f"S3 {method} {key}: {e}")

    def get_bytes(self, key):
        """Small object contents, or None if it doesn't exist (yet)."""
        try:
            return self._request("GET", key)[2]
        except RuntimeError as e:
            if "HTTP 404" in str(e):
                return None
            raise

    def put_bytes(self, key, data):
        self._request("PUT", key, body=data)

    def upload(self, local, key, label="upload"):
        size = os.path.getsize(local)
        if size <= PART_SIZE:
            with open(local, "rb") as f:
                self.put_bytes(key, f.read())
            return
        _, _, xml = self._request("POST", key, query={"uploads": ""})
        upload_id = re.search(rb"<UploadId>([^<]+)</UploadId>", xml).group(1).decode()
        parts, n = [], math.ceil(size / PART_SIZE)
        try:
            with open(local, "rb") as f:
                for i in range(1, n + 1):
                    chunk = f.read(PART_SIZE)
                    _, hdrs, _ = self._request("PUT", key, query={"partNumber": i, "uploadId": upload_id},
                                               body=chunk)
                    etag = {k.lower(): v for k, v in hdrs.items()}.get("etag", "")
                    parts.append((i, etag))
                    say(f"  {label}: {min(i * PART_SIZE, size) / 1e6:.0f} / {size / 1e6:.0f} MB")
            body = "<CompleteMultipartUpload>" + "".join(
                f"<Part><PartNumber>{i}</PartNumber><ETag>{e}</ETag></Part>" for i, e in parts
            ) + "</CompleteMultipartUpload>"
            self._request("POST", key, query={"uploadId": upload_id}, body=body.encode(),
                          headers={"content-type": "application/xml"})
        except Exception:
            try:
                self._request("DELETE", key, query={"uploadId": upload_id})
            except Exception:
                pass
            raise

    def download(self, key, local, progress=None):
        resp = self._request("GET", key, stream=True)
        total = int(resp.headers.get("Content-Length") or 0)
        done, tmp = 0, local + ".part"
        with open(tmp, "wb") as f:
            while True:
                buf = resp.read(1 << 20)
                if not buf:
                    break
                f.write(buf)
                done += len(buf)
                if progress:
                    progress(done, total)
        if total and done != total:
            raise RuntimeError(f"download incomplete ({done}/{total} bytes)")
        os.replace(tmp, local)
        return done

    def delete(self, key):
        try:
            self._request("DELETE", key)
        except Exception:
            pass

    def list(self, prefix):
        """[(key, age in seconds)] of every object under prefix (S3 ListObjectsV2, all pages)."""
        out, token = [], None
        while True:
            query = {"list-type": 2, "prefix": prefix}
            if token:
                query["continuation-token"] = token
            root = ET.fromstring(self._request("GET", "", query=query)[2])
            now = datetime.datetime.now(datetime.timezone.utc)
            token = None
            for el in root.iter():
                tag = el.tag.rsplit("}", 1)[-1]
                if tag == "Contents":
                    kids = {k.tag.rsplit("}", 1)[-1]: k.text for k in el}
                    try:
                        when = datetime.datetime.fromisoformat((kids.get("LastModified") or "").replace("Z", "+00:00"))
                        age = (now - when).total_seconds()
                    except ValueError:
                        age = 0.0
                    out.append((kids.get("Key", ""), age))
                elif tag == "NextContinuationToken":
                    token = el.text
            if not token:
                return out


# --------------------------------------------------------------------------- Runpod jobs API
class Endpoint:
    def __init__(self, cfg):
        self.base = f"https://api.runpod.ai/v2/{cfg['endpoint_id']}"
        self.key = cfg["api_key"]

    def _call(self, method, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Authorization": f"Bearer {self.key}",
                                              "Content-Type": "application/json",
                                              "User-Agent": USER_AGENT})
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, context=SSL, timeout=60) as r:
                    return json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    body = e.read().decode(errors="replace")[:200]
                    if "error code: 10" in body:
                        die(f"Cloudflare blocked the Runpod API request ({body.strip()}). Tell Claude the error code.")
                    die(f"Runpod refused the API key ({e.code}). Check api_key in {CONFIG_PATH}.")
                if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise RuntimeError(f"Runpod {path}: HTTP {e.code} {e.read().decode(errors='replace')[:300]}")
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt < 4:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise RuntimeError(f"Runpod {path}: {e}")

    def submit(self, inp):
        return self._call("POST", "/run", {"input": inp})["id"]

    def status(self, job_id):
        return self._call("GET", f"/status/{job_id}")

    def cancel(self, job_id):
        try:
            self._call("POST", f"/cancel/{job_id}")
        except Exception:
            pass

    def wait(self, job_id, label, timeout=3600):
        t0, last = time.time(), None
        while time.time() - t0 < timeout:
            st = self.status(job_id)
            s = st.get("status")
            if s != last:
                say(f"  {label}: {s.lower().replace('_', ' ')}  ({fmt_time(time.time() - t0)})")
                last = s
            if s in ("COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"):
                return st
            time.sleep(4)
        die(f"{label} did not finish within {timeout // 60} min")


def job_error(st):
    out = st.get("output")
    if isinstance(out, dict) and out.get("error"):
        return out["error"]
    return st.get("error") or f"job ended {st.get('status')}"


def push_worker_code(vol):
    for name in ("start.sh", "handler.py"):
        with open(os.path.join(HERE, "worker", name), "rb") as f:
            vol.put_bytes(f"farm/{name}", f.read())


# --------------------------------------------------------------------------- local Blender prep
PREP_SCRIPT = r"""
import bpy, os, sys, json, re
SIM_DIR = %r
out = sys.argv[sys.argv.index('--') + 1]
job = sys.argv[sys.argv.index('--') + 2]
removed = []
relocated = []
FONT_DIRS = [os.path.expanduser('~/Library/Fonts'), '/Library/Fonts', '/System/Library/Fonts', '/System/Library/Fonts/Supplemental']

def relocate(path, lib=None):
    # the file at its saved path, or the same filename found elsewhere on this Mac, or None
    p = os.path.normpath(bpy.path.abspath(path, library=lib))
    if os.path.exists(p):
        return p
    name = os.path.basename(p)
    if not name:
        return None
    for d in FONT_DIRS:
        q = os.path.join(d, name)
        if os.path.isfile(q):
            return q
    try:
        import subprocess
        out = subprocess.run(['mdfind', '-name', name], capture_output=True, text=True, timeout=8).stdout.splitlines()
        hits = [h for h in out if os.path.basename(h) == name and os.path.isfile(h)]
        if hits:
            return max(hits, key=os.path.getmtime)
    except Exception:
        pass
    return None

# the .blend may have been moved since these paths were saved: point them at the files where they are now
for coll in (bpy.data.fonts, bpy.data.sounds, bpy.data.images):
    for item in coll:
        if getattr(item, 'library', None) or getattr(item, 'packed_file', None) or not item.filepath or item.filepath == '<builtin>':
            continue
        if coll is bpy.data.images and item.source not in {'FILE', 'MOVIE'}:
            continue
        found = relocate(item.filepath)
        if found and os.path.normpath(bpy.path.abspath(item.filepath)) != found:
            relocated.append([item.name, found])
            item.filepath = found
for im in list(bpy.data.images):
    if im.source in {'FILE', 'SEQUENCE', 'MOVIE'} and not im.packed_file and im.filepath:
        p = bpy.path.abspath(im.filepath, library=im.library)
        if not os.path.exists(p):
            removed.append(im.name)
            bpy.data.images.remove(im)
warn = []
try:
    bpy.ops.file.pack_all()
except Exception as e:
    warn.append('pack: %s' % e)
# Blender can't pack videos: upload them with the job and point the copy at the farm volume
assets = []
for im in bpy.data.images:
    if im.packed_file or not im.filepath or im.library:
        continue
    if im.source == 'MOVIE':
        p = os.path.normpath(bpy.path.abspath(im.filepath))
        if os.path.isfile(p):
            key = '%02d_%s' % (len(assets), re.sub(r'[^A-Za-z0-9_.-]+', '_', os.path.basename(p)))
            assets.append([p, key])
            im.filepath = '/runpod-volume/jobs/%s/assets/%s' % (job, key)
    elif im.source == 'SEQUENCE':
        warn.append('image sequence not uploaded (render may miss it): ' + im.name)
if bpy.data.libraries:
    warn.append('linked libraries are not packed: ' + ', '.join(l.name for l in bpy.data.libraries))
s = bpy.context.scene
r = s.render
try:
    sys.path.insert(0, SIM_DIR)
    from sim_check import find_sims
    sims = find_sims()
except Exception as e:
    sims = []
    warn.append('simulation check failed: %s' % e)
bpy.ops.wm.save_as_mainfile(filepath=out, copy=True, compress=True, relative_remap=False)
print('FARMINFO' + json.dumps(dict(sims=sims, needs_order=any(not x['baked'] for x in sims), start=s.frame_start, end=s.frame_end, fps=r.fps / r.fps_base,
      w=r.resolution_x * r.resolution_percentage // 100, h=r.resolution_y * r.resolution_percentage // 100,
      engine=r.engine, removed=removed, relocated=relocated, warn=warn, assets=assets)))
"""


def find_blender():
    for p in (os.environ.get("BLENDER"), shutil.which("blender"),
              "/Applications/Blender.app/Contents/MacOS/Blender"):
        if p and os.path.exists(p):
            return p
    return None


def prepare(blend, tmpdir, job):
    blender = find_blender()
    if not blender:
        return None, None
    out = os.path.join(tmpdir, "scene.blend")
    script = os.path.join(tmpdir, "prep.py")
    with open(script, "w") as f:
        f.write(PREP_SCRIPT.replace("SIM_DIR = %r", "SIM_DIR = " + repr(HERE)))
    p = subprocess.run([blender, "-b", blend, "--python", script, "--", out, job],
                       capture_output=True, text=True, timeout=900)
    m = re.search(r"FARMINFO(\{.*\})", p.stdout)
    if not m or not os.path.exists(out):
        die("local Blender could not prepare the file:\n" + "\n".join((p.stdout + p.stderr).splitlines()[-15:]))
    return out, json.loads(m.group(1))


# --------------------------------------------------------------------------- commands
def cmd_setup(_args):
    cfg = load_config()
    say("Render farm setup — keys are saved to " + CONFIG_PATH + " (only readable by you).\n")
    say("Runpod console → Settings → API Keys → Create (read/write).")
    k = getpass.getpass("  Runpod API key (hidden, Enter to keep current): ").strip()
    if k:
        cfg["api_key"] = k
    say("\nRunpod console → Settings → S3 API Keys → Create. The access key starts with user_, the secret with rps_.")
    k = input(f"  S3 access key [{cfg.get('s3_access_key', '')}]: ").strip()
    if k:
        cfg["s3_access_key"] = k
    k = getpass.getpass("  S3 secret (hidden, Enter to keep current): ").strip()
    if k:
        cfg["s3_secret_key"] = k
    k = input(f"\n  Endpoint id [{cfg.get('endpoint_id', '')}]: ").strip()
    if k:
        cfg["endpoint_id"] = k
    fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)
    say("\n✓ saved. Next:  python3 farm.py test")


def cmd_test(_args):
    cfg = load_config()
    need_keys(cfg)
    vol, ep = Volume(cfg), Endpoint(cfg)
    say("Uploading worker code to the farm storage…")
    push_worker_code(vol)
    say("✓ storage OK\nStarting a GPU worker (the very first start installs Blender on the farm: ~3–6 min)…")
    st = ep.wait(ep.submit({"op": "ping"}), "test job", timeout=1800)
    if st.get("status") != "COMPLETED" or "error" in (st.get("output") or {}):
        die("worker test failed: " + str(job_error(st)))
    o = st["output"]
    say(f"\n✓ farm is working\n  GPU:     {o['gpu']}\n  Blender: {o['blender']}\n"
        f"  ffmpeg:  {'yes' if o['ffmpeg'] else 'MISSING'}\n  storage: {o['volume_free_gb']} GB free")


def cmd_render(args):
    cfg = load_config()
    need_keys(cfg)
    blend = os.path.abspath(os.path.expanduser(args.blend))
    if not os.path.exists(blend):
        die(f"file not found: {blend}")
    workers = max(1, args.workers or cfg["default_workers"])
    vol, ep = Volume(cfg), Endpoint(cfg)
    ui = bool(os.environ.get("FARM_UI"))
    now_phase = [None]

    def phase(name):
        # the page shows what the farm is doing right now: preparing, uploading, starting, rendering, stitching, downloading
        if ui and name != now_phase[0]:
            now_phase[0] = name
            say("FARMPHASE " + name)

    phase("preparing")
    job = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", os.path.splitext(os.path.basename(blend))[0])

    with tempfile.TemporaryDirectory() as tmp:
        upload_path, info = (blend, None) if args.no_pack else prepare(blend, tmp, job)
        if upload_path is None:
            upload_path = blend
            say("! Blender not found on this Mac, uploading the file as-is (set BLENDER=/path/to/Blender)")
        if info:
            if info["removed"]:
                say(f"! skipped {len(info['removed'])} missing image(s): {', '.join(info['removed'][:6])}")
            for name, found in info.get("relocated", []):
                say(f"! {name} wasn't where the file expected, using {found}")
            for w in info["warn"]:
                say("! " + w)
        start = args.start if args.start is not None else (info or {}).get("start")
        end = args.end if args.end is not None else (info or {}).get("end")
        fps = args.fps or (info or {}).get("fps") or 30
        if start is None or end is None:
            die("couldn't read the frame range; pass --start and --end")
        if info and (start < info["start"] or end > info["end"]):
            die(f"this file only has frames {info['start']}–{info['end']}; frames outside that range don't exist in the "
                "animation. Extend the range in Blender first.")
        n = end - start + 1
        size = f"{info['w']}x{info['h']}" if info else "render"
        loop = not args.no_loop
        sub = max(1, args.sub)
        batches = [(a, min(a + sub - 1, end)) for a in range(start, end + 1, sub)]
        sims = (info or {}).get("sims") or []
        unbaked = [x for x in sims if not x["baked"]]
        ordered = bool(unbaked) and not args.ignore_sims
        say(f"{os.path.basename(blend)}: frames {start}–{end} ({n}) · {size} · {fps:g} fps"
            + (f" · {info['engine']}" if info else ""))
        if ordered:
            names = ", ".join(sorted({f"{x['kind']} on {x['object']}" for x in unbaked})[:4])
            say(f"! This scene has simulations that aren't baked ({names}). Each frame builds on the one before, "
                "so the frames are drawn in order on one GPU. Bake them in Blender to use all your GPUs.")
            say("FARMMODE ordered")
            workers = 1
        else:
            workers = min(workers, len(batches))
            say("FARMMODE parallel")
        say(f"{len(batches)} small batches of ≤{sub} frames, shared by {workers} GPU{'s' if workers != 1 else ''}\n")

        phase("uploading")
        say("Uploading…")
        push_worker_code(vol)
        vol.upload(upload_path, f"jobs/{job}/scene.blend", label="scene")
        assets = (info or {}).get("assets") or []
        for a_path, a_key in assets:
            vol.upload(a_path, f"jobs/{job}/assets/{a_key}", label=os.path.basename(a_path))
        say(f"✓ uploaded ({(os.path.getsize(upload_path) + sum(os.path.getsize(a[0]) for a in assets)) / 1e6:.1f} MB"
            + (f", incl. {len(assets)} video(s) used as textures" if assets else "") + ")\n")

    # ---- render: each GPU runs one long Blender session that keeps taking the next unclaimed batch
    done_frames, gpu_secs, t0 = 0, 0.0, time.time()
    gpus, per_frame = set(), []
    price = float(cfg["price_per_gpu_hour"])
    want = set(range(start, end + 1))

    # per-batch state for the web UI (printed as FARMSTATE lines when FARM_UI is set)
    ui = bool(os.environ.get("FARM_UI"))
    cstate = {b: {"start": b[0], "end": b[1], "log": None, "status": "queued", "worker": None, "slot": None,
                  "gpu": None, "since": None, "frames_done": 0} for b in batches}
    bkey = lambda b: f"{b[0]:04d}-{b[1]:04d}"
    by_key = {bkey(b): b for b in batches}
    owner = {}          # batch -> slot that last worked on it
    last_emit = [None]

    def emit():
        if ui:
            s = json.dumps(list(cstate.values()))
            if s != last_emit[0]:
                last_emit[0] = s
                say("\nFARMSTATE " + s)  # leading newline: the progress bar line has none

    if ui:
        say("FARMJOB " + job)

    slots = {}          # slot -> {"jid", "since" (submitted), "ran" (started working)}
    extra_jobs = []     # the video-making job, so a cancel can stop it too
    attempts = {i: 0 for i in range(workers)}
    relaunches = [0]

    def launch(slot):
        jid = ep.submit({"op": "pull", "job": job, "slot": slot, "start": start, "end": end, "sub": sub,
                         "ordered": ordered})
        slots[slot] = {"jid": jid, "since": time.time(), "ran": None}

    def cancel_all(*_):
        say("\nCancelling…")
        for sj in slots.values():
            ep.cancel(sj["jid"])
        for jid in extra_jobs:
            ep.cancel(jid)
        die("cancelled — nothing more will bill")

    signal.signal(signal.SIGINT, cancel_all)
    phase("starting")
    atexit.register(lambda: [ep.cancel(sj["jid"]) for sj in list(slots.values())])  # a crash must never leave GPUs billing
    for i in range(workers):
        launch(i)
    last_line = ""
    while True:
        time.sleep(3)
        # 1. what the GPUs have finished, and which batch each one is on now
        have = {int(m.group(1)) for k, _ in vol.list(f"jobs/{job}/frames/") for m in [re.search(r"f_(\d+)\.png$", k)] if m}
        claims = {k.rsplit("/", 1)[-1]: age for k, age in vol.list(f"jobs/{job}/claims/")}
        current = {}
        for i in slots:
            data = vol.get_bytes(f"jobs/{job}/workers/w{i}.json")
            try:
                current[i] = json.loads(data)["batch"] if data else None
            except (ValueError, KeyError, TypeError):
                current[i] = None
        missing = want - have
        done_frames = len(want & have)

        # 2. job outcomes: a GPU that stops early (error, timeout) is replaced, a clean finish just ends its session
        for slot, sj in list(slots.items()):
            st = ep.status(sj["jid"])
            s = st.get("status")
            if s == "IN_PROGRESS" and sj["ran"] is None:
                sj["ran"] = time.time()
            if s not in ("COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"):
                continue
            slots.pop(slot)
            gpu_secs += (st.get("executionTime") or 0) / 1000
            out = st.get("output") if isinstance(st.get("output"), dict) else {}
            if s == "COMPLETED" and "error" not in out:
                gpus.add(out.get("gpu", "?"))
                if out.get("frames"):
                    per_frame.append(out.get("sec_per_frame", 0))
            else:
                if "unknown op" in str(job_error(st)):
                    for o in slots.values():
                        ep.cancel(o["jid"])
                    die("The GPU workers are still running the old farm code. Release the endpoint's workers in the "
                        "Runpod console (Serverless → your endpoint → Workers → Release), then render again.")
                attempts[slot] += 1
                if attempts[slot] > PULL_RETRIES:
                    for o in slots.values():
                        ep.cancel(o["jid"])
                    die(f"GPU {slot + 1} failed {PULL_RETRIES + 1} times:\n{job_error(st)}")
                say(f"\n! GPU {slot + 1} stopped ({str(job_error(st))[:200]}), starting another")
                if missing:
                    launch(slot)

        # 3. claims nobody is touching belong to a crashed GPU: free them so another GPU takes the frames
        stale = [c for c, age in claims.items() if age > CLAIM_STALE and c in by_key
                 and not all(f in have for f in range(by_key[c][0], by_key[c][1] + 1))]
        for c in stale:
            vol.delete(f"jobs/{job}/claims/{c}")
            claims.pop(c)
        if missing and not slots:
            free = [c for c, b in by_key.items() if c not in claims and not all(f in have for f in range(b[0], b[1] + 1))]
            if free:  # work nobody holds and nobody is running: start a GPU for it
                relaunches[0] += 1
                if relaunches[0] > 3 * max(workers, 1):
                    die("the GPUs keep stopping before the frames are finished")
                launch(0 if ordered else min(range(workers), key=lambda i: attempts[i]))

        # 4. per-batch state for the page
        for b, c in cstate.items():
            c["frames_done"] = sum(1 for f in range(b[0], b[1] + 1) if f in have)
            c["since"] = None
            if c["frames_done"] == b[1] - b[0] + 1:
                c.update(status="done")
            else:
                c.update(status="queued", slot=None, worker=None, log=None)
        for i, name in current.items():
            b = by_key.get(name)
            if b:
                owner[b] = i
        for b, i in owner.items():
            cstate[b].update(slot=i, worker=f"GPU {i + 1}", log=f"w{i}")
        for i, name in current.items():
            b = by_key.get(name)
            if b and cstate[b]["status"] != "done":
                cstate[b]["status"] = "rendering"
        idle_slots = [i for i, name in current.items() if i in slots and (not by_key.get(name) or cstate[by_key[name]]["status"] == "done")]
        queued = [b for b in batches if cstate[b]["status"] == "queued"]
        for i in sorted(idle_slots):
            if i not in slots:
                continue
            if queued and slots[i]["ran"] is None:   # still starting up: show it holding the next batch
                b = queued.pop(0)
                cstate[b].update(status="waiting for a GPU", slot=i, worker=f"GPU {i + 1}", log=f"w{i}", since=slots[i]["since"])
        emit()

        el = time.time() - t0
        live = sum(time.time() - sj["ran"] for sj in slots.values() if sj["ran"])
        cost = (gpu_secs + live) / 3600 * price
        busy = sum(1 for sj in slots.values() if sj["ran"])
        eta = (el / done_frames * (n - done_frames)) if done_frames else 0
        bar = "#" * int(20 * done_frames / n) + "-" * (20 - int(20 * done_frames / n))
        line = (f"[{bar}] {done_frames}/{n} frames · {busy} GPUs busy · {fmt_time(el)} elapsed"
                + (f" · ~{fmt_time(eta)} left" if done_frames else " · starting GPUs…") + f" · ~${cost:.2f}")
        if done_frames or busy:
            phase("rendering")
        if line != last_line:
            print("\r" + line + " " * 4, end="", flush=True)
            last_line = line
        if not missing:
            break
    for sj in slots.values():   # every frame exists: stop any GPU that is still starting up
        ep.cancel(sj["jid"])
    emit()
    say("\n")

    # ---- stitch + download
    name = f"{stem}_{size}_{int(round((n - (1 if loop else 0)) / fps))}s"
    phase("stitching")
    stitch_jid = ep.submit({"op": "stitch", "job": job, "start": start, "end": end, "fps": fps,
                            "loop": loop, "name": name, "crf": args.crf,
                            "keep_frames": args.keep_frames})
    extra_jobs.append(stitch_jid)
    st = ep.wait(stitch_jid, "making the MP4")
    out = st.get("output") if isinstance(st.get("output"), dict) else {}
    if st.get("status") != "COMPLETED" or "error" in out:
        die("MP4 step failed: " + str(job_error(st)))
    gpu_secs += (st.get("executionTime") or 0) / 1000
    local_mp4 = os.path.join(args.out or os.path.dirname(blend), f"{name}.mp4")
    phase("downloading")
    say("Downloading…")
    last_pct = [-1]

    def dl_progress(done, total):
        pct = int(100 * done / total) if total else 0
        if ui and pct != last_pct[0]:
            last_pct[0] = pct
            say(f"FARMDL {pct}")
    vol.download(out["video"], local_mp4, progress=dl_progress)
    vol.delete(out["video"])
    if not args.keep_frames:
        vol.delete(f"jobs/{job}/scene.blend")
        for _, a_key in assets:
            vol.delete(f"jobs/{job}/assets/{a_key}")

    total = time.time() - t0
    seam = out.get("seam")
    say(f"\n✓ done in {fmt_time(total)}  →  {local_mp4}")
    say(f"  {out['frames']} frames @ {fps:g} fps · {out['bytes'] / 1e6:.1f} MB · GPUs: {', '.join(sorted(gpus))}")
    if per_frame:
        say(f"  ~{sum(per_frame) / len(per_frame):.1f} s per frame per GPU (incl. Blender start-up)")
    if seam:
        say(f"  loop seam: {'PASS' if seam['pass'] else 'CHECK'} (frame {start} vs {end}: PSNR {seam['psnr_db']} dB)")
    say(f"  GPU time ~{gpu_secs / 60:.1f} min ≈ ${gpu_secs / 3600 * price:.2f} (estimate; worker start-up also bills)")


def main():
    ap = argparse.ArgumentParser(prog="farm.py", description="Render a .blend on Runpod GPUs in parallel.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="save your Runpod keys")
    sub.add_parser("test", help="check the GPU workers")
    r = sub.add_parser("render", help="render a .blend to MP4")
    r.add_argument("blend")
    r.add_argument("--workers", type=int, help="GPUs to use at once (default 4)")
    r.add_argument("--sub", type=int, default=2, help="frames per small batch a GPU takes at a time (default 2)")
    r.add_argument("--ignore-sims", action="store_true", help="render in parallel even if a simulation isn't baked")
    r.add_argument("--start", type=int)
    r.add_argument("--end", type=int)
    r.add_argument("--fps", type=float)
    r.add_argument("--no-loop", action="store_true", help="keep the last frame (not a seamless loop)")
    r.add_argument("--crf", type=int, default=16, help="H.264 quality, lower = better (default 16)")
    r.add_argument("--out", help="folder for the MP4 (default: next to the .blend)")
    r.add_argument("--keep-frames", action="store_true", help="leave the PNG frames on the farm storage")
    r.add_argument("--no-pack", action="store_true", help="upload the file exactly as it is")
    args = ap.parse_args()
    {"setup": cmd_setup, "test": cmd_test, "render": cmd_render}[args.cmd](args)


if __name__ == "__main__":
    main()
