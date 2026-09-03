"""Walk a decoded Packet Tracer XML file and print devices + IPs.

Usage:
    python examples/inspect_devices.py activity.xml
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


def main(path: str) -> int:
    xml = Path(path).read_text(encoding="utf-8", errors="replace")
    devices = re.findall(r"<DEVICE>(.*?)</DEVICE>", xml, re.DOTALL)
    seen: set[str] = set()
    for body in devices:
        name_m = re.search(r"<NAME[^>]*>([^<]+)</NAME>", body)
        type_m = re.search(r'<TYPE[^>]*model="([^"]*)"[^>]*>([^<]+)</TYPE>', body)
        if not name_m:
            continue
        name = name_m.group(1)
        if name in seen:
            continue
        seen.add(name)

        model = type_m.group(1) if type_m else "?"
        kind = type_m.group(2) if type_m else "?"
        print(f"[{name}] {kind} (model={model})")

        # Pull IPs from RUNNING_CONFIG lines if present, else from device end-host fields.
        for ip_m in re.finditer(r"<LINE>\s*ip address (\S+) (\S+)</LINE>", body):
            print(f"    cli: {ip_m.group(1)} / {ip_m.group(2)}")
        for ip_m in re.finditer(r"<IP>([^<]+)</IP>", body):
            print(f"    ip:  {ip_m.group(1)}")
        gw = re.search(r"<GATEWAY>([^<]*)</GATEWAY>", body)
        if gw and gw.group(1):
            print(f"    gw:  {gw.group(1)}")
        print()
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: inspect_devices.py <decoded.xml>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
