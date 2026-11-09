# Render farm

Renders any `.blend` on several Runpod RTX 4090s at once and gives you back the MP4.
It puts the frames in one shared pile of small batches. Every GPU keeps one Blender session
open and takes the next batch whenever it is free, so all of them work until the last frame.
A GPU that crashes is replaced. Then the frames are joined into one MP4.
If the scene has simulations that aren't baked (cloth, smoke, particles, rigid bodies), the
frames must be drawn in order, so it uses one GPU. Bake them in Blender to use all of them.
For seamless loops it drops the duplicate last frame and checks the seam.

## One-time setup

1. In the Runpod console, go to **Settings → API Keys** and create a key with read/write access.
2. Go to **Settings → S3 API Keys** and create a key. Copy both parts: the access key
   (`user_…`) and the secret (`rps_…`). The secret is only shown once.
3. In Terminal:
   ```
   cd render-farm
   python3 farm.py setup
   python3 farm.py test
   ```
   The first `test` installs Blender on the farm. That takes about 3–6 minutes, one time only.

## Render

```
python3 farm.py render "/path/to/Court_Rave_mylogo.blend"
```
The MP4 lands next to the `.blend`. Options:

| Option | What it does |
|---|---|
| `--workers 4` | GPUs to use at once (max 10 on the endpoint) |
| `--sub 2` | Frames a GPU takes at a time (default 2) |
| `--ignore-sims` | Use all GPUs even if a simulation isn't baked |
| `--no-loop` | Keep the last frame, for clips that aren't seamless loops |
| `--start 1 --end 100` | Render part of the timeline |
| `--crf 16` | Video quality (lower = better and bigger) |
| `--out ~/Desktop` | Where to save the MP4 |
| `--keep-frames` | Leave the PNG frames on the farm storage |
| `--no-pack` | Upload the file exactly as it is |

Before uploading, it opens the file in your local Blender to pack in textures and fonts,
and it skips images whose files are missing (like that leftover Megascans gloss map).
Press **Ctrl+C** to cancel. In-flight GPU jobs get cancelled, so nothing keeps billing.

## What it costs

- **GPU time:** $1.10 per GPU-hour, billed per second, only while rendering.
  Idle costs $0.
- **Storage:** the 30 GB farm volume is about $2.10/month.

## What runs where

| Part | Where |
|---|---|
| `farm.py` | Your Mac: upload, share frames between GPUs, track progress, download |
| `worker/start.sh`, `worker/handler.py` | Runpod GPU workers (uploaded automatically) |
| Endpoint `bassmint-render-farm` (`a3u5mshjs6dp6v`) | RTX 4090 pool, US-IL-1, 0–10 workers |
| Network volume `bassmint-render-farm` (`dqrdx4v647`) | Shared storage, US-IL-1 |
