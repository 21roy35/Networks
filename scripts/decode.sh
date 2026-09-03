#!/usr/bin/env bash
#
# Decode a single Cisco Packet Tracer file (.pka/.pkt) to XML and print a
# device/IP summary. Convenience wrapper around the vendored pka2xml decoder.
#
# Usage:
#   scripts/decode.sh <input.pka> [output.xml]
#
# If output.xml is omitted, the XML is written next to the input file.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKA_TOOL_DIR="$REPO_ROOT/tools/pka2xml"

if [ "$#" -lt 1 ]; then
  echo "usage: scripts/decode.sh <input.pka> [output.xml]" >&2
  exit 2
fi

IN="$1"
OUT="${2:-${IN%.*}.xml}"

if [ ! -f "$PKA_TOOL_DIR/pka2xml/libtwofish.so" ] && [ ! -f "$PKA_TOOL_DIR/pka2xml/libtwofish.dylib" ]; then
  python3 "$PKA_TOOL_DIR/build_libtwofish.py"
fi

PYTHONPATH="$PKA_TOOL_DIR" python3 -m pka2xml decode "$IN" "$OUT"

echo
echo "Device summary for $OUT:"
python3 "$PKA_TOOL_DIR/examples/inspect_devices.py" "$OUT"
