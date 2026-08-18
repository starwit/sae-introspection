#!/usr/bin/env bash

# Reads frame data from running SAE / Valkey using sae-echo and saves
# them as jpeg files while removing all time-related metadata

set -euo pipefail

if [ $# -ne 1 ]; then
  echo "usage: $(basename "$0") OUTPUT_DIR" >&2
  exit 1
fi

# Check up front, otherwise a missing tool only shows up as a per-frame warning
# from the pipeline below (whose stderr is suppressed)
check_requirements() {
  local missing=0 cmd hint

  for cmd in sae-echo jq jpegtran base64 sha256sum cut mktemp touch; do
    if command -v "$cmd" >/dev/null 2>&1; then
      continue
    fi

    case "$cmd" in
      sae-echo) hint="pipx install git+https://github.com/starwit/sae-introspection.git" ;;
      jq) hint="apt install jq" ;;
      jpegtran) hint="apt install libjpeg-turbo-progs" ;;
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
mkdir -p "$OUTDIR"
FIXED_TIME="200001010000"   # touch -t format: YYYYMMDDhhmm[.ss]

bash -c "$SOURCE_CMD" | while IFS= read -r line; do
  [ -z "$line" ] && continue   # skip stray blank lines

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
