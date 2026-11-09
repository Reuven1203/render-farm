#!/usr/bin/env python3
"""
Delete finished renders' leftovers (frames, scene copies, logs) from the farm's network volume.

    python3 cleanup_volume.py              # dry run: shows what would be deleted
    python3 cleanup_volume.py --yes        # delete it

Only the jobs/ folder is touched; farm/ (Blender and the worker code) is never deleted. A job folder that
changed in the last --idle minutes (default 15) is skipped, so a render that is running now is safe.
"""
import argparse
import collections
import os
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import farm


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="actually delete (without it, only show what would go)")
    ap.add_argument("--idle", type=float, default=15, help="skip job folders changed in the last N minutes (default 15)")
    args = ap.parse_args()

    vol = farm.Volume(farm.load_config())
    jobs = collections.defaultdict(list)
    for key, age in vol.list("jobs/"):
        jobs[key.split("/")[1]].append((key, age))

    doomed, kept = [], []
    for job, items in sorted(jobs.items()):
        if min(age for _, age in items) > args.idle * 60:
            doomed += [key for key, _ in items]
        else:
            kept.append(job)
    print(f"{len(doomed)} files in {len(jobs) - len(kept)} old job folders; skipping {len(kept)} recent: {', '.join(kept) or 'none'}")
    if not args.yes:
        print("Dry run. Nothing deleted. Run again with --yes to delete.")
        return
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(vol.delete, doomed))
    left = vol.list("jobs/")
    print(f"Done. jobs/ now holds {len(left)} files.")


if __name__ == "__main__":
    main()
