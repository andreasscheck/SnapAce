#!/usr/bin/env bash
# Bakes this repo's klipper/extras/{ace,filament_feed}.py and
# klipper/kinematics/extruder.py into a SnapmakerU1-Extended-Firmware
# build, as the `andreasscheck` mod overlay.
#
# Process:
#   1. checkout firmware  - clone/fetch paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware
#   2. extract image      - `./dev.sh make extract` the stock firmware for that tag
#   3. add changes        - 3-way merge our customizations onto the current stock
#                            files and regenerate the mod overlay's patches
#   4. create image        - `./dev.sh make build PROFILE=extended-andreasscheck`
#
# Usage:
#   scripts/build_custom_firmware.sh [firmware-tag]
#
# Env overrides:
#   FIRMWARE_REPO_DIR   where to clone/reuse the firmware repo
#                        (default: ../SnapmakerU1-Extended-Firmware, sibling of this repo)
#   MOD_NAME             mod overlay name under overlays/mods/ (default: andreasscheck)

set -euo pipefail

SNAPACE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIRMWARE_TAG="${1:-v1.5.2-paxx12-21}"
FIRMWARE_REPO_URL="https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware.git"
FIRMWARE_REPO_DIR="${FIRMWARE_REPO_DIR:-$(cd "$SNAPACE_DIR/.." && pwd)/SnapmakerU1-Extended-Firmware}"
MOD_NAME="${MOD_NAME:-andreasscheck}"

echo ">> [1/4] checkout firmware: $FIRMWARE_REPO_URL @ $FIRMWARE_TAG"
if [[ ! -d "$FIRMWARE_REPO_DIR/.git" ]]; then
  git clone "$FIRMWARE_REPO_URL" "$FIRMWARE_REPO_DIR"
fi
git -C "$FIRMWARE_REPO_DIR" fetch --tags --quiet
git -C "$FIRMWARE_REPO_DIR" checkout --quiet "$FIRMWARE_TAG"
git -C "$FIRMWARE_REPO_DIR" submodule update --init --recursive

FIRMWARE_VERSION="$(sed -n 's/^FIRMWARE_VERSION=//p' "$FIRMWARE_REPO_DIR/vars.mk")"
STOCK_DIR="$FIRMWARE_REPO_DIR/tmp/extracted-$FIRMWARE_VERSION/rootfs"

echo ">> [2/4] extract image: stock firmware $FIRMWARE_VERSION"
( cd "$FIRMWARE_REPO_DIR" && ./dev.sh make tools )
if [[ ! -d "$STOCK_DIR" ]]; then
  ( cd "$FIRMWARE_REPO_DIR" && ./dev.sh make extract )
fi

echo ">> [3/4] add changes: merge SnapAce onto stock $FIRMWARE_VERSION, regenerate overlay"
OVERLAY_DIR="$FIRMWARE_REPO_DIR/overlays/mods/$MOD_NAME/10-snapace"
mkdir -p "$OVERLAY_DIR/patches" "$OVERLAY_DIR/root/home/lava/klipper/klippy/extras"

cp "$SNAPACE_DIR/klipper/extras/ace.py" "$OVERLAY_DIR/root/home/lava/klipper/klippy/extras/ace.py"

merge_and_patch() {
  local rel_path="$1"       # path under klipper/, e.g. klippy/extras/filament_feed.py
  local snapace_file="$2"   # SnapAce repo file to merge in
  local snapace_baseline="$3"  # SnapAce repo's original/ (pre-modification) baseline
  local patch_name="$4"

  local stock_file="$STOCK_DIR/home/lava/klipper/$rel_path"
  local work_dir
  work_dir="$(mktemp -d)"
  trap 'rm -rf "$work_dir"' RETURN

  cp "$snapace_file" "$work_dir/merged"
  if ! git merge-file -L "SnapAce" -L "old-stock" -L "stock-$FIRMWARE_VERSION" \
      "$work_dir/merged" "$snapace_baseline" "$stock_file"; then
    echo "!! Conflicts merging $rel_path onto stock $FIRMWARE_VERSION."
    echo "   Resolve manually: $work_dir/merged (has <<<<<<< markers), then re-run."
    exit 1
  fi
  python3 -m py_compile "$work_dir/merged"

  mkdir -p "$work_dir/rootfs.original/home/lava/klipper/$(dirname "$rel_path")"
  mkdir -p "$work_dir/rootfs/home/lava/klipper/$(dirname "$rel_path")"
  cp "$stock_file" "$work_dir/rootfs.original/home/lava/klipper/$rel_path"
  cp "$work_dir/merged" "$work_dir/rootfs/home/lava/klipper/$rel_path"

  # diff exits 1 when the files differ (the expected case here) and only
  # 2+ on a real error, but pipefail treats any non-zero as a failure -
  # fold the expected 1 into success so the pipe doesn't trip `set -e`.
  ( cd "$work_dir" && diff -u \
      "rootfs.original/home/lava/klipper/$rel_path" \
      "rootfs/home/lava/klipper/$rel_path" || [[ $? -eq 1 ]] ) \
    | sed "1s#.*#--- rootfs.original/home/lava/klipper/$rel_path#; \
           2s#.*#+++ rootfs/home/lava/klipper/$rel_path#" \
    > "$OVERLAY_DIR/patches/$patch_name.patch"
}

merge_and_patch "klippy/extras/filament_feed.py" \
  "$SNAPACE_DIR/klipper/extras/filament_feed.py" \
  "$SNAPACE_DIR/original/extras/filament_feed.py" \
  "01-filament-feed-ace-support"

merge_and_patch "klippy/kinematics/extruder.py" \
  "$SNAPACE_DIR/klipper/kinematics/extruder.py" \
  "$SNAPACE_DIR/original/kinematics/extruder.py" \
  "02-extruder-ace-feed-assist"

echo ">> [3/4] add changes: ACE status UI (/ace)"
UI_OVERLAY_DIR="$FIRMWARE_REPO_DIR/overlays/mods/$MOD_NAME/20-ace-ui"
mkdir -p "$UI_OVERLAY_DIR/root/etc/nginx/fluidd.d" \
         "$UI_OVERLAY_DIR/root/usr/local/ace-ui/html" \
         "$UI_OVERLAY_DIR/root/usr/local/share/firmware-config/functions"

cp "$SNAPACE_DIR/ace-ui/nginx/ace.conf" "$UI_OVERLAY_DIR/root/etc/nginx/fluidd.d/ace.conf"
cp "$SNAPACE_DIR/ace-ui/html/"* "$UI_OVERLAY_DIR/root/usr/local/ace-ui/html/"
cp "$SNAPACE_DIR/ace-ui/firmware-config/12_links_ace_status.yaml" \
  "$UI_OVERLAY_DIR/root/usr/local/share/firmware-config/functions/12_links_ace_status.yaml"

echo ">> [3/4] add changes: remove unused tool-change stubs (T4-T31)"
TOOLS_OVERLAY_DIR="$FIRMWARE_REPO_DIR/overlays/mods/$MOD_NAME/30-limit-tool-count"
mkdir -p "$TOOLS_OVERLAY_DIR/patches"

FLUIDD_REL="home/lava/origin_printer_data/config/fluidd.cfg"
FLUIDD_STOCK="$STOCK_DIR/$FLUIDD_REL"
work_dir="$(mktemp -d)"
mkdir -p "$work_dir/rootfs.original/$(dirname "$FLUIDD_REL")" \
         "$work_dir/rootfs/$(dirname "$FLUIDD_REL")"
cp "$FLUIDD_STOCK" "$work_dir/rootfs.original/$FLUIDD_REL"
python3 "$SNAPACE_DIR/scripts/remove_unused_tool_macros.py" \
  "$FLUIDD_STOCK" "$work_dir/rootfs/$FLUIDD_REL"

( cd "$work_dir" && diff -u \
    "rootfs.original/$FLUIDD_REL" "rootfs/$FLUIDD_REL" || [[ $? -eq 1 ]] ) \
  | sed "1s#.*#--- rootfs.original/$FLUIDD_REL#; 2s#.*#+++ rootfs/$FLUIDD_REL#" \
  > "$TOOLS_OVERLAY_DIR/patches/01-remove-unused-tool-macros.patch"
rm -rf "$work_dir"

echo ">> [4/4] create image: build extended-$MOD_NAME"
OUTPUT_FILE="firmware/U1_extended-$MOD_NAME.bin"
rm -f "$FIRMWARE_REPO_DIR/$OUTPUT_FILE"

# create_firmware.sh restores tmp/cache-chroot into $ROOTFS_DIR/cache before
# applying overlays and saves it back after. On a second+ build with that
# cache warm, some overlay step silently ends up installing ~148 fewer
# files (~10MB smaller rootfs) - a structurally valid but incomplete image
# the printer refuses to accept. Clearing it before each build costs
# nothing (it's rebuilt from this run) and avoids the failure outright.
rm -rf "$FIRMWARE_REPO_DIR/tmp/cache-chroot"
( cd "$FIRMWARE_REPO_DIR" && DOCKER_OPTS="--privileged" \
    ./dev.sh sudo make build "PROFILE=extended-$MOD_NAME" "OUTPUT_FILE=$OUTPUT_FILE" )

echo ">> Done: $FIRMWARE_REPO_DIR/$OUTPUT_FILE"

# A stale tmp/cache-chroot (or another persisted build cache) has once
# before produced a silently truncated rootfs - ~10MB/148 files smaller,
# still a structurally valid .bin, but one the printer refused to accept.
# Flag a meaningfully smaller image than the previous build before it gets
# to that stage.
SIZE_RECORD="$FIRMWARE_REPO_DIR/firmware/.$MOD_NAME-last-size"
NEW_SIZE="$(wc -c < "$FIRMWARE_REPO_DIR/$OUTPUT_FILE" | tr -d ' ')"

if [[ -f "$SIZE_RECORD" ]]; then
  PREV_SIZE="$(cat "$SIZE_RECORD")"
  THRESHOLD=$(( PREV_SIZE * 98 / 100 ))
  if [[ "$NEW_SIZE" -lt "$THRESHOLD" ]]; then
    DELTA_MB=$(( (PREV_SIZE - NEW_SIZE) / 1024 / 1024 ))
    echo ""
    echo "!! WARNING: $OUTPUT_FILE is ${DELTA_MB}MB smaller than the previous build"
    echo "!!   ($NEW_SIZE vs $PREV_SIZE bytes). That has previously meant a"
    echo "!!   partial rootfs from stale build caches, not a real change -"
    echo "!!   verify before flashing, or rebuild clean:"
    echo "!!     rm -rf '$FIRMWARE_REPO_DIR/tmp' && bash '${BASH_SOURCE[0]}' $FIRMWARE_TAG"
    echo ""
  fi
fi
echo "$NEW_SIZE" > "$SIZE_RECORD"
