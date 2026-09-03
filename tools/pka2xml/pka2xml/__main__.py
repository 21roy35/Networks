"""Command-line interface for pka2xml.

Usage:
    python -m pka2xml decode  input.pka  output.xml
    python -m pka2xml encode  input.xml  output.pka
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import PkaError, decrypt_pka, encrypt_pka


def _read(path: Path) -> bytes:
    return Path(path).read_bytes()


def _write(path: Path, data: bytes) -> None:
    Path(path).write_bytes(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pka2xml",
        description="Convert Cisco Packet Tracer .pka/.pkt files to/from XML",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_dec = sub.add_parser("decode", help="Decrypt .pka/.pkt into XML")
    p_dec.add_argument("input", type=Path, help="Path to .pka or .pkt file")
    p_dec.add_argument("output", type=Path, help="Output XML path")

    p_enc = sub.add_parser("encode", help="Encrypt XML back into .pka/.pkt")
    p_enc.add_argument("input", type=Path, help="Input XML path")
    p_enc.add_argument("output", type=Path, help="Output .pka or .pkt path")

    args = parser.parse_args(argv)

    try:
        if args.cmd == "decode":
            xml = decrypt_pka(_read(args.input))
            _write(args.output, xml)
            print(f"[+] wrote {args.output} ({len(xml):,} bytes)", file=sys.stderr)
        elif args.cmd == "encode":
            blob = encrypt_pka(_read(args.input))
            _write(args.output, blob)
            print(f"[+] wrote {args.output} ({len(blob):,} bytes)", file=sys.stderr)
    except PkaError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
