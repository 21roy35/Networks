# Vendored tool: pka2xml

This directory vendors a clean-room, MIT-licensed decoder for Cisco Packet
Tracer `.pka`/`.pkt` files so that the network topologies in this repository can
be inspected offline, without the proprietary Packet Tracer application.

- Source: [`jeamxn/cisco-pka-to-xml`](https://github.com/jeamxn/cisco-pka-to-xml)
  (MIT). See `LICENSE`.
- File format originally documented by Mirco De Zorzi —
  [`pka2xml`](https://github.com/mircodz/pka2xml).
- Twofish reference implementation by Niels Ferguson, vendored under
  `vendor/twofish/` (MIT). See `vendor/twofish/LICENSE`.

It is vendored (rather than fetched at install time) because upstream
reverse-engineering repositories are frequently taken down, and a durable
Cloud Agent environment should not depend on their continued availability.

## Usage

```bash
# one-time: build the native Twofish helper
python3 tools/pka2xml/build_libtwofish.py

# decode a file
PYTHONPATH=tools/pka2xml python3 -m pka2xml decode input.pka output.xml
```

This tool is for inspecting files you have the right to read. It does not
bypass any license check or activity-grading mechanism.
