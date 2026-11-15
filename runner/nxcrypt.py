"""Encrypted pieces between the PC and the build machine.

Everything that travels through the tunnel is AES-256-GCM: a piece is a run of frames,
each frame = 4-byte plaintext length (big endian) + 12-byte nonce + ciphertext + 16-byte tag.
The frame's position (stream name, piece number, frame number) is the associated data, so
frames can't be swapped, dropped or replayed into another place without failing."""
import os
import struct

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

FRAME = 4 * 1024 * 1024          # plaintext bytes per frame
PIECE = 48 * 1024 * 1024         # plaintext bytes per HTTP request (under the tunnel's 100 MB body cap)


def _aad(stream: str, piece: int, frame: int) -> bytes:
    return f"{stream}|{piece}|{frame}".encode()


def seal(key: bytes, stream: str, piece: int, data: bytes) -> bytes:
    """Encrypts one piece (any length up to PIECE) into frames."""
    g = AESGCM(key)
    out = bytearray()
    n = 0
    for off in range(0, max(len(data), 1), FRAME):
        chunk = data[off:off + FRAME]
        nonce = os.urandom(12)
        out += struct.pack(">I", len(chunk)) + nonce + g.encrypt(nonce, chunk, _aad(stream, piece, n))
        n += 1
    return bytes(out)


def open_(key: bytes, stream: str, piece: int, blob: bytes) -> bytes:
    """Decrypts one piece; raises if anything was changed or cut off."""
    g = AESGCM(key)
    out = bytearray()
    pos, n = 0, 0
    while pos < len(blob):
        if pos + 16 > len(blob):
            raise ValueError("cut-off frame")
        (length,) = struct.unpack(">I", blob[pos:pos + 4])
        nonce = blob[pos + 4:pos + 16]
        end = pos + 16 + length + 16
        if end > len(blob):
            raise ValueError("cut-off frame")
        out += g.decrypt(nonce, blob[pos + 16:end], _aad(stream, piece, n))
        pos, n = end, n + 1
    return bytes(out)
