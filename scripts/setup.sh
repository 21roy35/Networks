#!/usr/bin/env bash
#
# Idempotent development-environment bootstrap for this repository.
#
# This repo holds Cisco Packet Tracer coursework: encrypted binary `.pka`
# activity files plus PDF/DOCX write-ups. Packet Tracer itself is proprietary
# and cannot be installed non-interactively, so the "application" work here is
# reading and inspecting the network topologies stored inside the `.pka` files.
#
# This script prepares everything an agent needs to do that offline:
#   1. install system tooling for reading the document artifacts (poppler-utils)
#   2. build the vendored Twofish shared library used by the pka2xml decoder
#   3. extract the committed `.zip` archives
#   4. decode every `.pka` into readable XML under build/decoded/
#
# It is safe to run repeatedly: every step overwrites or skips existing output.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PKA_TOOL_DIR="$REPO_ROOT/tools/pka2xml"
BUILD_DIR="$REPO_ROOT/build"
EXTRACT_DIR="$BUILD_DIR/extracted"
DECODED_DIR="$BUILD_DIR/decoded"

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

log "1/4 System packages (poppler-utils, unzip)"
missing=()
command -v pdftotext >/dev/null 2>&1 || missing+=("poppler-utils")
command -v unzip >/dev/null 2>&1 || missing+=("unzip")
if [ "${#missing[@]}" -gt 0 ]; then
  if command -v sudo >/dev/null 2>&1; then
    sudo apt-get update -qq && sudo apt-get install -y -qq "${missing[@]}"
  else
    apt-get update -qq && apt-get install -y -qq "${missing[@]}"
  fi
else
  echo "already present: pdftotext, unzip"
fi

log "2/4 Build vendored Twofish library for pka2xml"
python3 "$PKA_TOOL_DIR/build_libtwofish.py"

log "3/4 Extract committed archives"
mkdir -p "$EXTRACT_DIR"
shopt -s nullglob
for zip in "$REPO_ROOT"/*.zip; do
  dest="$EXTRACT_DIR/$(basename "${zip%.zip}")"
  mkdir -p "$dest"
  unzip -o -q "$zip" -d "$dest"
  echo "extracted $(basename "$zip") -> ${dest#$REPO_ROOT/}"
done

log "4/4 Decode Packet Tracer .pka files to XML"
mkdir -p "$DECODED_DIR"
found_pka=0
while IFS= read -r -d '' pka; do
  found_pka=1
  out="$DECODED_DIR/$(basename "${pka%.pka}").xml"
  PYTHONPATH="$PKA_TOOL_DIR" python3 -m pka2xml decode "$pka" "$out"
done < <(find "$EXTRACT_DIR" -type f -name '*.pka' -print0)
if [ "$found_pka" -eq 0 ]; then
  echo "no .pka files found under ${EXTRACT_DIR#$REPO_ROOT/}"
fi

log "Setup complete"
echo "Decoded topologies: ${DECODED_DIR#$REPO_ROOT/}/"
echo "Inspect a topology: scripts/decode.sh <file.pka>  |  python3 tools/pka2xml/examples/inspect_devices.py build/decoded/<name>.xml"
