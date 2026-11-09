"""
Run inside Blender (blender -b file.blend --python audit_blend.py).

Prints one FARMPROBE line: the scene's frame range / size, and an audit of every external file
the scene uses, sorted by what the farm will do with it:

  ok           packed into the .blend before upload (images, fonts, sounds) or sent with the job (videos)
  relocated    not where the .blend expects it, but found elsewhere on this Mac and used
  missing      the file isn't on this Mac, so the render will go without it
  unsupported  Blender can't pack it, so it will most likely be missing on the farm
"""
import json
import os

import bpy


FONT_DIRS = [os.path.expanduser("~/Library/Fonts"), "/Library/Fonts", "/System/Library/Fonts", "/System/Library/Fonts/Supplemental"]


def locate(path, lib=None):
    """Where the file really is: its saved path, or the same filename found elsewhere on this Mac (the .blend may
    have been moved or uploaded since the path was saved). None if it can't be found."""
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
        out = subprocess.run(["mdfind", "-name", name], capture_output=True, text=True, timeout=8).stdout.splitlines()
        hits = [h for h in out if os.path.basename(h) == name and os.path.isfile(h)]
        if hits:
            return max(hits, key=os.path.getmtime)
    except Exception:
        pass
    return None


def on_disk(path, lib=None):
    return locate(path, lib) is not None


def check(kind, item, count_key):
    """Count a file as ok; if it had to be found somewhere else, also say where."""
    found = locate(item.filepath, getattr(item, "library", None))
    if found is None:
        add(kind, item.name, "missing", item.filepath)
        return
    ok[count_key] += 1
    if found != os.path.normpath(bpy.path.abspath(item.filepath)):
        add(kind, item.name, "relocated", found)


ok = {"images": 0, "fonts": 0, "sounds": 0, "videos": 0}
issues = []


def add(kind, name, status, detail):
    issues.append({"kind": kind, "name": name, "status": status, "detail": detail})


def guarded(fn):
    try:
        fn()
    except Exception as e:  # an unusual file must never stop the audit
        add("audit", fn.__name__, "unsupported", f"could not check: {e}")


def images():
    for im in bpy.data.images:
        # linked images belong to their library, which is reported on its own
        if im.library or im.source not in {"FILE", "SEQUENCE", "MOVIE"} or not im.filepath:
            continue
        if im.packed_file:
            ok["images"] += 1
        elif im.source == "SEQUENCE":
            add("image sequence", im.name, "unsupported", im.filepath)
        else:
            check("video" if im.source == "MOVIE" else "image", im, "videos" if im.source == "MOVIE" else "images")


def fonts():
    for f in bpy.data.fonts:
        if f.library or f.filepath in ("", "<builtin>"):
            continue
        if f.packed_file:
            ok["fonts"] += 1
        else:
            check("font", f, "fonts")


def sounds():
    for sd in bpy.data.sounds:
        if sd.library or not sd.filepath:
            continue
        if sd.packed_file:
            ok["sounds"] += 1
        else:
            check("sound", sd, "sounds")


def libraries():
    for lib in bpy.data.libraries:
        add("linked .blend", lib.name, "missing" if not on_disk(lib.filepath) else "unsupported", lib.filepath)


def caches():
    for cf in bpy.data.cache_files:
        add("Alembic/USD cache", cf.name, "missing" if not on_disk(cf.filepath) else "unsupported", cf.filepath)
    for v in bpy.data.volumes:
        if v.filepath and not v.packed_file and not v.library:
            add("volume (VDB)", v.name, "missing" if not on_disk(v.filepath) else "unsupported", v.filepath)
    for mc in bpy.data.movieclips:
        if mc.filepath and not mc.library:
            add("movie clip", mc.name, "missing" if not on_disk(mc.filepath) else "unsupported", mc.filepath)


def disk_caches():
    for ob in bpy.data.objects:
        pcs = [ps.point_cache for ps in ob.particle_systems]
        pcs += [m.point_cache for m in ob.modifiers if hasattr(m, "point_cache") and m.point_cache]
        if any(pc.use_disk_cache for pc in pcs):
            add("simulation cache", ob.name, "unsupported", "stored on disk, not inside the .blend")
        for m in ob.modifiers:
            ds = getattr(m, "domain_settings", None)
            if ds and getattr(ds, "cache_directory", ""):
                add("fluid cache", ob.name, "unsupported", ds.cache_directory)


def simulations():
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from sim_check import find_sims
    sims.extend(find_sims())


sims = []
for step in (images, fonts, sounds, libraries, caches, disk_caches, simulations):
    guarded(step)

s = bpy.context.scene
r = s.render
print("FARMPROBE" + json.dumps({
    "start": s.frame_start, "end": s.frame_end, "fps": r.fps / r.fps_base,
    "w": r.resolution_x * r.resolution_percentage // 100, "h": r.resolution_y * r.resolution_percentage // 100,
    "engine": r.engine,
    "sims": sims, "needs_order": any(not x["baked"] for x in sims),
    "audit": {"ok": ok, "issues": issues[:60], "issue_count": len(issues)},
}))
