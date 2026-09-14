#!/usr/bin/env bash

# Reads frame data from running SAE / Valkey using sae-echo and saves
# them as jpeg files while removing all time-related metadata.
# Frames older than MAX_AGE_DAYS are deleted from OUTPUT_DIR.

set -euo pipefail

if [ $# -ne 2 ]; then
  echo "usage: $(basename "$0") OUTPUT_DIR MAX_AGE_DAYS" >&2
  exit 1
fi

if ! [[ "$2" =~ ^[0-9]+$ ]]; then
  echo "error: MAX_AGE_DAYS must be a non-negative integer, got '$2'" >&2
  exit 1
fi

# Check up front, otherwise a missing tool only shows up as a per-frame warning
# from the pipeline below (whose stderr is suppressed)
check_requirements() {
  local missing=0 cmd hint

  for cmd in sae-echo jq jpegtran base64 sha256sum cut mktemp touch find; do
    if command -v "$cmd" >/dev/null 2>&1; then
      continue
    fi

    case "$cmd" in
      sae-echo) hint="pipx install git+https://github.com/starwit/sae-introspection.git" ;;
      jq) hint="apt install jq" ;;
      jpegtran) hint="apt install libjpeg-turbo-progs" ;;
      find) hint="apt install findutils" ;;
      *) hint="apt install coreutils" ;;
    esac

    echo "error: required command '$cmd' not found (try: $hint)" >&2
    missing=1
  done

  return $missing
}

check_requirements || exit 1

SOURCE_CMD="set -o pipefail; sae-echo -f | jq -r .frame.frameDataJpeg"
OUTDIR="$1"
MAX_AGE_DAYS="$2"
mkdir -p "$OUTDIR"
FIXED_TIME="200001010000"   # touch -t format: YYYYMMDDhhmm[.ss]
CLEANUP_INTERVAL=3600       # seconds between cleanup runs

# mtime is pinned to FIXED_TIME, so age is judged by ctime instead: it is set
# to "now" by the mv/touch below and can't be forged. A frame that is seen
# again gets re-saved and thus a fresh ctime.
cleanup_old_frames() {
  find "$OUTDIR" -maxdepth 1 -type f -name '*.jpg' -cmin +$((MAX_AGE_DAYS * 24 * 60)) -delete \
    || echo "warn: cleanup of old frames failed" >&2
}

cleanup_old_frames
last_cleanup=$SECONDS

bash -c "$SOURCE_CMD" | while IFS= read -r line; do
  [ -z "$line" ] && continue   # skip stray blank lines

  if (( SECONDS - last_cleanup >= CLEANUP_INTERVAL )); then
    cleanup_old_frames
    last_cleanup=$SECONDS
  fi

  tmp=$(mktemp)
  if ! printf '%s' "$line" | base64 -d 2>/dev/null | jpegtran -copy none -optimize > "$tmp" 2>/dev/null; then
    echo "warn: failed to decode/strip a frame, skipping" >&2
    rm -f "$tmp"
    continue
  fi

  name=$(sha256sum "$tmp" | cut -d' ' -f1)
  dest="${OUTDIR}/${name}.jpg"

  mv "$tmp" "$dest"
  touch -t "$FIXED_TIME" "$dest"
done
