#!/usr/bin/env bash
# Render farm: runs on every Runpod worker when it boots.
#
# The first worker that ever starts installs Blender, ffmpeg's libraries and the
# Runpod SDK onto the shared network volume (one time, a few minutes). Every
# later worker just unpacks them from the volume, which takes seconds.
set -uo pipefail

V=/runpod-volume/farm
BL_VER=5.2.0
BL_DIR="blender-${BL_VER}-linux-x64"
BL_TAR="$V/${BL_DIR}.tar"
READY="$V/.ready-${BL_VER}"
PKGS="ffmpeg libegl1 libgl1 libglx-mesa0 libxi6 libxkbcommon0 libsm6 libxrender1 libxxf86vm1 libxfixes3 libx11-6 libxext6"

log() { echo "[start] $*"; }

bootstrap() {
  log "first run on this volume: installing Blender ${BL_VER} + libraries (one time)"
  mkdir -p "$V/debs/partial" "$V/wheels"
  apt-get update -qq || return 1
  apt-get install -y -qq --download-only -o Dir::Cache::archives="$V/debs" $PKGS || return 1
  pip download -q -d "$V/wheels" runpod || return 1
  log "downloading Blender ${BL_VER}"
  curl -fsSL -o "$V/blender.tar.xz" \
    "https://download.blender.org/release/Blender${BL_VER%.*}/${BL_DIR}.tar.xz" || return 1
  # keep it as one plain .tar: one big file reads fast off the network volume
  xz -d -T0 "$V/blender.tar.xz" && mv "$V/blender.tar" "$BL_TAR" || return 1
  touch "$READY"
  log "volume ready"
}

mkdir -p "$V"
if [ ! -f "$READY" ]; then
  if mkdir "$V/.lock" 2>/dev/null; then
    bootstrap
    ok=$?
    rmdir "$V/.lock" 2>/dev/null
    [ $ok -eq 0 ] || { log "bootstrap failed"; exit 1; }
  else
    log "another worker is installing; waiting for it"
    for _ in $(seq 1 300); do [ -f "$READY" ] && break; sleep 5; done
    if [ ! -f "$READY" ]; then
      log "install lock looks stale, taking over"
      rmdir "$V/.lock" 2>/dev/null
      mkdir "$V/.lock" 2>/dev/null && { bootstrap; rmdir "$V/.lock" 2>/dev/null; }
      [ -f "$READY" ] || exit 1
    fi
  fi
fi

# per-worker setup (local disk, fast)
shopt -s nullglob
debs=("$V"/debs/*.deb)
if [ ${#debs[@]} -gt 0 ]; then
  dpkg -i --force-depends "${debs[@]}" >/tmp/dpkg.log 2>&1 || log "dpkg reported warnings (see /tmp/dpkg.log)"
fi
python3 -c "import runpod" 2>/dev/null || pip install -q --no-index --find-links "$V/wheels" runpod
[ -x "/opt/${BL_DIR}/blender" ] || tar -xf "$BL_TAR" -C /opt

export BLENDER_BIN="/opt/${BL_DIR}/blender"
log "worker ready: $(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)"
exec python3 -u "$V/handler.py"
