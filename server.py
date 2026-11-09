#!/usr/bin/env python3
"""
Drag-and-drop web UI for the render farm.

    python3 server.py            # then open http://127.0.0.1:8765

Drop a .blend on the page: it is saved to ./uploads and inspected with your local
Blender (frame range, fps, size). You review it, press Start, and `farm.py render`
renders it on Runpod; the MP4 lands in ./renders. Standard library only.
Listens on localhost only. Runpod keys come from ~/.bassmint_farm.json (written by `farm.py setup`).
"""
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import farm

HERE = os.path.dirname(os.path.abspath(__file__))
UPLOADS = os.path.join(HERE, "uploads")
RENDERS = os.path.join(HERE, "renders")
STATS_PATH = os.path.join(HERE, "stats.json")
CONFIG_PATH = os.path.expanduser("~/.bassmint_farm.json")
PORT = int(os.environ.get("PORT", 8765))
KEY_FIELDS = ("api_key", "s3_access_key", "s3_secret_key")
MAX_ACTIVE_JOBS = 2

JOBS = {}
STAGED = {}
LOCK = threading.Lock()
PROGRESS_RE = re.compile(r"\[[#-]+\] (\d+)/(\d+) frames · (\d+) GPUs busy.*?~\$([\d.]+)")
DONE_RE = re.compile(r"✓ done in (\S+)\s+→\s+(.+)")
SUMMARY_RE = re.compile(r"(\d+) frames @ ([\d.]+) fps · ([\d.]+) MB · GPUs: (.+)")
SPF_RE = re.compile(r"~([\d.]+) s per frame per GPU")
COST_RE = re.compile(r"≈ \$([\d.]+)")
SEAM_RE = re.compile(r"loop seam: (\w+)")
PROBE_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit_blend.py")
# lines in a chunk's Blender log that mean a file was not found while rendering
WARN_RE = re.compile(r"(?i)(cannot read|can't read|unable to open|could not open|failed to (open|load)|file not found|not found|missing)")


# --------------------------------------------------------------------------- config / keys
def read_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def keys_ready():
    cfg = read_config()
    return all(cfg.get(k) for k in KEY_FIELDS)


# --------------------------------------------------------------------------- cost estimate
def read_stats():
    try:
        with open(STATS_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"spf": None, "n": 0}


def record_spf(value):
    st = read_stats()
    st["spf"] = value if not st.get("spf") else round(0.7 * st["spf"] + 0.3 * value, 3)
    st["n"] = st.get("n", 0) + 1
    with open(STATS_PATH, "w") as f:
        json.dump(st, f)


# --------------------------------------------------------------------------- helpers
def safe_name(name):
    base = os.path.basename(name)
    stem, ext = os.path.splitext(base)
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", stem) + ext.lower()


def probe_blend(path):
    blender = farm.find_blender()
    if not blender:
        return None
    try:
        p = subprocess.run([blender, "-b", path, "--python", PROBE_SCRIPT],
                           capture_output=True, text=True, timeout=120)
        m = re.search(r"FARMPROBE(\{.*\})", p.stdout)
        return json.loads(m.group(1)) if m else None
    except Exception:
        return None


def scan_render_logs(job):
    """What Blender said about missing files while rendering, read from the chunk logs on the farm volume.
    Returns None when the logs couldn't be read."""
    fj, chunks = job.get("farm_job"), job.get("chunks") or []
    if not fj or not chunks:
        return None
    try:
        vol = farm.Volume(farm.load_config())
        found = []
        for log in sorted({c.get("log") for c in chunks if c.get("log")}):
            key = f"jobs/{fj}/logs/{log}.log"
            data = vol.get_bytes(key)
            for line in (data or b"").decode(errors="replace").splitlines():
                line = re.sub(r"^\S+\s+\w+\s+\|\s*", "", line).strip()[:200]
                if WARN_RE.search(line) and "Saved:" not in line and "Fra:" not in line and line not in found:
                    found.append(line)
            vol.delete(key)
        return found[:8]
    except (Exception, SystemExit):
        return None


def active_jobs():
    return sum(1 for j in JOBS.values() if j["status"] in ("queued", "running", "cancelling"))


def start_job(name, path, opts):
    cmd = [sys.executable, "-u", os.path.join(HERE, "farm.py"), "render", path, "--out", RENDERS,
           "--workers", str(max(1, min(10, int(opts.get("workers", 4))))),
           "--crf", str(max(0, min(35, int(opts.get("crf", 16)))))]
    if not opts.get("loop", True):
        cmd.append("--no-loop")
    for k in ("start", "end"):
        if str(opts.get(k, "")).lstrip("-").isdigit():
            cmd += [f"--{k}", str(opts[k])]
    jid = uuid.uuid4().hex[:8]
    job = {"name": name, "status": "queued", "log": [], "started": time.time(), "path": path, "opts": opts}
    JOBS[jid] = job
    threading.Thread(target=run_job, args=(job, cmd), daemon=True).start()
    return jid


def run_job(job, cmd):
    try:
        env = dict(os.environ, PYTHONUNBUFFERED="1", FARM_UI="1")
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                             start_new_session=True)
        job["proc"] = p
        job["status"] = "running"
        buf = b""
        while True:
            chunk = p.stdout.read1(4096)
            if not chunk:
                break
            buf += chunk
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for raw in parts:
                line = raw.decode(errors="replace").rstrip()
                if not line:
                    continue
                m = PROGRESS_RE.search(line)
                if m:
                    job["progress"] = {"done": int(m[1]), "total": int(m[2]), "gpus": int(m[3]),
                                       "cost": float(m[4]), "line": line}
                    continue
                if line.startswith("FARMSTATE "):
                    job["chunks"] = json.loads(line[10:])
                    continue
                if line.startswith("FARMPHASE "):
                    job["phase"] = line[10:].strip()
                    continue
                if line.startswith("FARMDL "):
                    job["download"] = int(line[7:].strip() or 0)
                    continue
                if line.startswith("FARMMODE "):
                    job["mode"] = line[9:].strip()
                    continue
                if line.startswith("FARMJOB "):
                    job["farm_job"] = line[8:].strip()
                    continue
                d = DONE_RE.search(line)
                if d:
                    job["output"] = os.path.basename(d[2].strip())
                    job["duration"] = d[1]
                s = SUMMARY_RE.search(line)
                if s:
                    job["result"] = {"frames": int(s[1]), "fps": float(s[2]), "mb": float(s[3]), "gpus": s[4]}
                s = SPF_RE.search(line)
                if s:
                    job["spf"] = float(s[1])
                s = COST_RE.search(line)
                if s:
                    job["final_cost"] = float(s[1])
                s = SEAM_RE.search(line)
                if s:
                    job["seam"] = s[1]
                with LOCK:
                    job["log"].append(line)
        p.wait()
        succeeded = p.returncode == 0 and job.get("output") and job["status"] != "cancelling"
        if succeeded:
            if job.get("spf"):
                record_spf(job["spf"])
            job["render_check"] = scan_render_logs(job)  # before the status flips, so the page sees it with "done"
        job["status"] = "cancelled" if job["status"] == "cancelling" else "done" if succeeded else "failed"
    except Exception as e:
        job["log"].append(f"server error: {e}")
        job["status"] = "failed"
    job["finished"] = time.time()


class Handler(BaseHTTPRequestHandler):
    server_version = "RenderFarm/2.0"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _same_origin(self):
        origin = self.headers.get("Origin")
        return not origin or urllib.parse.urlparse(origin).netloc == self.headers.get("Host")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def _public(self, jid, job, since=0):
        with LOCK:
            log = job["log"][since:]
        return {"id": jid, "name": job["name"], "status": job["status"], "progress": job.get("progress"),
                "output": job.get("output"), "chunks": self._chunks(job), "log": log, "log_len": since + len(log),
                "started": job["started"], "finished": job.get("finished"), "opts": job.get("opts"),
                "duration": job.get("duration"), "result": job.get("result"), "final_cost": job.get("final_cost"),
                "seam": job.get("seam"), "mode": job.get("mode"), "phase": job.get("phase"), "download": job.get("download"), "render_check": job.get("render_check")}

    def _chunks(self, job):
        return job.get("chunks", [])

    def _staged_public(self, sid, st):
        return {"id": sid, "name": st["name"], "size": st["size"], "info": st["info"]}

    # -- GET
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/":
            with open(os.path.join(HERE, "ui.html"), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/state":
            self._json({"keys_ready": keys_ready(), "price": farm.load_config()["price_per_gpu_hour"],
                        "stats": read_stats(), "max_active": MAX_ACTIVE_JOBS,
                        "staged": [self._staged_public(i, s) for i, s in STAGED.items()],
                        "jobs": [self._public(i, j) for i, j in sorted(JOBS.items(), key=lambda x: -x[1]["started"])]})
        elif u.path.startswith("/api/job/"):
            jid = u.path.rsplit("/", 1)[1]
            job = JOBS.get(jid)
            if not job:
                return self._json({"error": "unknown job"}, 404)
            self._json(self._public(jid, job, int(q.get("since", ["0"])[0])))
        elif u.path.startswith("/api/console/"):
            # live Blender output of one GPU, read from the farm's network volume
            job = JOBS.get(u.path.rsplit("/", 1)[1])
            if not job or not job.get("farm_job"):
                return self._json({"text": ""})
            try:
                key = q["key"][0] if "key" in q else ""
                if not re.fullmatch(r"w\d+", key):
                    return self._json({"text": ""})
                data = farm.Volume(farm.load_config()).get_bytes(f"jobs/{job['farm_job']}/logs/{key}.log")
            except Exception as ex:
                return self._json({"text": "", "error": str(ex)[:200]})
            lines = (data or b"").decode(errors="replace").splitlines()
            self._json({"text": "\n".join(lines[-200:])})
        elif u.path.startswith("/renders/"):
            path = os.path.join(RENDERS, os.path.basename(urllib.parse.unquote(u.path)))
            if not os.path.isfile(path):
                return self._json({"error": "not found"}, 404)
            size = os.path.getsize(path)
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f'inline; filename="{os.path.basename(path)}"')
            self.end_headers()
            with open(path, "rb") as f:
                while chunk := f.read(1 << 20):
                    self.wfile.write(chunk)
        else:
            self._json({"error": "not found"}, 404)

    # -- POST
    def do_POST(self):
        if not self._same_origin():
            return self._json({"error": "bad origin"}, 403)
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        tail = u.path.rsplit("/", 1)[1]
        if u.path.startswith("/api/cancel/"):
            job = JOBS.get(tail)
            if job and job.get("proc") and job["status"] == "running":
                job["status"] = "cancelling"
                job["proc"].send_signal(signal.SIGINT)  # farm.py cancels in-flight GPU jobs on SIGINT
            return self._json({"ok": True})
        if u.path == "/api/stage":
            return self._stage(q)
        if u.path == "/api/stage-path":
            return self._stage_path(self._body())
        if u.path.startswith("/api/start/"):
            return self._start(tail, self._body())
        if u.path.startswith("/api/rerun/"):
            job = JOBS.get(tail)
            if not job or not os.path.exists(job["path"]):
                return self._json({"error": "The original file is gone. Drop it again."}, 404)
            if active_jobs() >= MAX_ACTIVE_JOBS:
                return self._json({"error": f"{MAX_ACTIVE_JOBS} renders are already running. Wait for one to finish."}, 429)
            return self._json({"id": start_job(job["name"], job["path"], job["opts"] or {})})
        if u.path.startswith("/api/discard/"):
            st = STAGED.pop(tail, None)
            if st and not st.get("external") and os.path.exists(st["path"]):
                os.remove(st["path"])  # only our own uploaded copies, never a file the user pointed us at
            return self._json({"ok": True})
        if u.path.startswith("/api/dismiss/"):
            job = JOBS.get(tail)
            if job and job["status"] not in ("queued", "running", "cancelling"):
                JOBS.pop(tail, None)
            return self._json({"ok": True})
        self._json({"error": "not found"}, 404)

    def _stage(self, q):
        name = safe_name(q.get("name", "scene.blend"))
        if not name.lower().endswith(".blend"):
            return self._json({"error": "That isn't a .blend file. Drop a file that ends in .blend."}, 400)
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return self._json({"error": "That file is empty."}, 400)
        sid = uuid.uuid4().hex[:8]
        path = os.path.join(UPLOADS, f"{sid}_{name}")
        left = length
        with open(path, "wb") as f:
            while left:
                chunk = self.rfile.read(min(left, 1 << 20))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        if left:
            os.remove(path)
            return self._json({"error": "The upload was interrupted. Drop the file again."}, 400)
        info = probe_blend(path)
        STAGED[sid] = {"name": name, "path": path, "size": length, "info": info}
        self._json(self._staged_public(sid, STAGED[sid]))

    def _stage_path(self, body):
        """The desktop app knows where a dropped file really lives, so render it in place."""
        path = os.path.realpath(os.path.expanduser(str(body.get("path", ""))))
        if not path.lower().endswith(".blend") or not os.path.isfile(path):
            return self._json({"error": "That isn't a .blend file. Drop a file that ends in .blend."}, 400)
        sid = uuid.uuid4().hex[:8]
        STAGED[sid] = {"name": safe_name(os.path.basename(path)), "path": path, "size": os.path.getsize(path),
                       "info": probe_blend(path), "external": True}
        self._json(self._staged_public(sid, STAGED[sid]))

    def _start(self, sid, opts):
        st = STAGED.get(sid)
        if not st:
            return self._json({"error": "That file is no longer staged. Drop it again."}, 404)
        if not keys_ready():
            return self._json({"error": "The farm is not set up on this Mac yet. Run farm.py setup."}, 400)
        if active_jobs() >= MAX_ACTIVE_JOBS:
            return self._json({"error": f"{MAX_ACTIVE_JOBS} renders are already running. Start this one when one finishes."}, 429)
        info = st.get("info") or {}
        if info.get("start") is not None:
            for k in ("start", "end"):
                val = str(opts.get(k, "")).strip()
                if val.lstrip("-").isdigit() and not info["start"] <= int(val) <= info["end"]:
                    return self._json({"error": f"This file only has frames {info['start']}–{info['end']}. "
                                                "To render more, extend the range in Blender first."}, 400)
        STAGED.pop(sid)
        self._json({"id": start_job(st["name"], st["path"], opts)})


def main():
    os.makedirs(UPLOADS, exist_ok=True)
    os.makedirs(RENDERS, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Render farm UI → http://127.0.0.1:{PORT}  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        for j in JOBS.values():
            if j.get("proc") and j["status"] == "running":
                j["proc"].send_signal(signal.SIGINT)


if __name__ == "__main__":
    main()
