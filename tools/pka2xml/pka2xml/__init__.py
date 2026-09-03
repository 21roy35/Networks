"""Cisco Packet Tracer .pka/.pkt -> XML decoder.

Implements the algorithm originally documented by Mirco De Zorzi in pka2xml:
    1. byte-position obfuscation
    2. Twofish-EAX decryption  (key = 0x89 * 16, iv = 0x10 * 16)
    3. byte-position obfuscation
    4. zlib decompression (4-byte big-endian length prefix)

Encryption is the same pipeline in reverse.

Twofish is provided by a small shared library built from Niels Ferguson's
reference C implementation (vendored under ``vendor/twofish``). EAX mode
(CMAC + CTR) is implemented in pure Python here so the only native dependency
is the block cipher itself.
"""
from __future__ import annotations

import ctypes
import os
import sys
import zlib
from ctypes import (
    CDLL,
    POINTER,
    Structure,
    c_char_p,
    c_int,
    c_uint32,
    create_string_buffer,
    pointer,
)
from pathlib import Path

__all__ = ["Twofish", "decrypt_pka", "encrypt_pka", "PkaError"]


class PkaError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Native Twofish bindings
# ---------------------------------------------------------------------------

class _TwofishKey(Structure):
    _fields_ = [("s", (c_uint32 * 4) * 256), ("K", c_uint32 * 40)]


def _find_library() -> str:
    """Locate the libtwofish shared library."""
    env = os.environ.get("LIBTWOFISH")
    if env and Path(env).exists():
        return env

    here = Path(__file__).resolve().parent
    names = ("libtwofish.so", "libtwofish.dylib", "libtwofish.dll")
    search_dirs = [
        here,
        here.parent,
        here.parent / "build",
        Path("/usr/local/lib"),
        Path("/usr/lib"),
    ]
    for d in search_dirs:
        for n in names:
            p = d / n
            if p.exists():
                return str(p)
    raise PkaError(
        "libtwofish shared library not found. Build it first with "
        "`python build_libtwofish.py` or set the LIBTWOFISH environment variable."
    )


_LIB = CDLL(_find_library())
_LIB.Twofish_initialise.argtypes = []
_LIB.Twofish_initialise.restype = None
_LIB.Twofish_prepare_key.argtypes = [c_char_p, c_int, POINTER(_TwofishKey)]
_LIB.Twofish_prepare_key.restype = None
_LIB.Twofish_encrypt.argtypes = [POINTER(_TwofishKey), c_char_p, c_char_p]
_LIB.Twofish_encrypt.restype = None
_LIB.Twofish_decrypt.argtypes = [POINTER(_TwofishKey), c_char_p, c_char_p]
_LIB.Twofish_decrypt.restype = None
_LIB.Twofish_initialise()


class Twofish:
    """Thin wrapper around the Twofish reference C implementation."""

    block_size = 16

    def __init__(self, key: bytes) -> None:
        if not (0 < len(key) <= 32):
            raise ValueError("Twofish key length must be between 1 and 32 bytes")
        self._key = _TwofishKey()
        _LIB.Twofish_prepare_key(key, len(key), pointer(self._key))

    def encrypt(self, block: bytes) -> bytes:
        if len(block) != 16:
            raise ValueError("Twofish block must be exactly 16 bytes")
        out = create_string_buffer(16)
        _LIB.Twofish_encrypt(pointer(self._key), block, out)
        return out.raw[:16]

    def decrypt(self, block: bytes) -> bytes:
        if len(block) != 16:
            raise ValueError("Twofish block must be exactly 16 bytes")
        out = create_string_buffer(16)
        _LIB.Twofish_decrypt(pointer(self._key), block, out)
        return out.raw[:16]


# ---------------------------------------------------------------------------
# EAX mode (CMAC + CTR) in pure Python
# ---------------------------------------------------------------------------

_BS = 16


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def _gf_double(b: bytes) -> bytes:
    """Double a 128-bit value over GF(2^128) (CMAC subkey derivation)."""
    n = (int.from_bytes(b, "big") << 1) & ((1 << 128) - 1)
    if b[0] & 0x80:
        n ^= 0x87
    return n.to_bytes(16, "big")


def _omac(cipher: Twofish, t: int, msg: bytes) -> bytes:
    """OMAC1 (CMAC) of ``t`` prefix block + ``msg``."""
    L = cipher.encrypt(b"\x00" * _BS)
    k1 = _gf_double(L)
    k2 = _gf_double(k1)

    data = t.to_bytes(_BS, "big") + msg
    if len(data) % _BS == 0:
        last = _xor(data[-_BS:], k1)
        body = data[:-_BS]
    else:
        pad_len = _BS - (len(data) % _BS)
        padded = data + b"\x80" + b"\x00" * (pad_len - 1)
        last = _xor(padded[-_BS:], k2)
        body = padded[:-_BS]

    state = b"\x00" * _BS
    for i in range(0, len(body), _BS):
        state = cipher.encrypt(_xor(state, body[i : i + _BS]))
    return cipher.encrypt(_xor(state, last))


def _ctr(cipher: Twofish, nonce_block: bytes, data: bytes) -> bytes:
    out = bytearray()
    base = int.from_bytes(nonce_block, "big")
    for i in range(0, len(data), _BS):
        ks = cipher.encrypt(((base + i // _BS) & ((1 << 128) - 1)).to_bytes(_BS, "big"))
        chunk = data[i : i + _BS]
        out += _xor(ks, chunk[: len(ks)])
    return bytes(out)


def _eax_decrypt(cipher: Twofish, nonce: bytes, ct_with_tag: bytes, header: bytes = b"") -> bytes:
    tag = ct_with_tag[-_BS:]
    ct = ct_with_tag[:-_BS]
    N = _omac(cipher, 0, nonce)
    H = _omac(cipher, 1, header)
    C = _omac(cipher, 2, ct)
    expected = _xor(_xor(N, H), C)
    if expected != tag:
        raise PkaError("EAX authentication tag mismatch — file may be corrupt or wrong key")
    return _ctr(cipher, N, ct)


def _eax_encrypt(cipher: Twofish, nonce: bytes, plaintext: bytes, header: bytes = b"") -> bytes:
    N = _omac(cipher, 0, nonce)
    H = _omac(cipher, 1, header)
    ct = _ctr(cipher, N, plaintext)
    C = _omac(cipher, 2, ct)
    tag = _xor(_xor(N, H), C)
    return ct + tag


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_PKA_KEY = bytes([0x89] * 16)
_PKA_IV = bytes([0x10] * 16)


def decrypt_pka(data: bytes) -> bytes:
    """Decrypt the contents of a ``.pka`` / ``.pkt`` file to raw XML."""
    n = len(data)

    # Stage 1: position-keyed XOR + reverse
    stage1 = bytes(
        (data[n + ~i] ^ ((n - i * n) & 0xFF)) & 0xFF for i in range(n)
    )

    # Stage 2: Twofish-EAX
    cipher = Twofish(_PKA_KEY)
    stage2 = _eax_decrypt(cipher, _PKA_IV, stage1)

    # Stage 3: position-keyed XOR (length-based)
    m = len(stage2)
    stage3 = bytes(stage2[i] ^ ((m - i) & 0xFF) for i in range(m))

    # Stage 4: zlib decompress (4-byte big-endian length prefix)
    if len(stage3) < 4:
        raise PkaError("Decrypted payload too small to contain length prefix")
    expected_len = int.from_bytes(stage3[:4], "big")
    try:
        plain = zlib.decompress(stage3[4:])
    except zlib.error as e:
        raise PkaError(f"zlib decompression failed: {e}") from e
    if len(plain) != expected_len:
        sys.stderr.write(
            f"warning: declared size {expected_len} != actual {len(plain)}\n"
        )
    return plain


def encrypt_pka(xml: bytes) -> bytes:
    """Encrypt raw XML back into ``.pka`` / ``.pkt`` format."""
    # Stage 4 inverse: zlib compress + 4-byte big-endian length prefix
    compressed = zlib.compress(xml, level=-1)
    stage3 = len(xml).to_bytes(4, "big") + compressed

    # Stage 3 inverse: position-keyed XOR
    m = len(stage3)
    stage2 = bytes(stage3[i] ^ ((m - i) & 0xFF) for i in range(m))

    # Stage 2 inverse: Twofish-EAX encrypt
    cipher = Twofish(_PKA_KEY)
    stage1 = _eax_encrypt(cipher, _PKA_IV, stage2)

    # Stage 1 inverse: reverse + position-keyed XOR
    n = len(stage1)
    out = bytearray(n)
    for i in range(n):
        out[n + ~i] = stage1[i] ^ ((n - i * n) & 0xFF)
    return bytes(out)
