"""Small standard-library PNG encoder/decoder used for generated textures."""

from __future__ import annotations

import struct
import zlib


def png_rgb(width: int, height: int, pixels: bytes) -> bytes:
    """Encode tightly packed RGB pixels as an 8-bit PNG."""
    stride = width * 3
    raw = b"".join(b"\x00" + pixels[row * stride:(row + 1) * stride] for row in range(height))
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">2I5B", width, height, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(raw, level=6)) + _chunk(b"IEND", b""))


def solid_png(rgb: tuple[int, int, int]) -> bytes:
    """Return a small, solid 8-bit RGB texture."""
    return png_rgb(8, 8, bytes(rgb) * 64)


def read_png(data: bytes) -> tuple[int, int, int, bytes]:
    """Decode a non-interlaced 8-bit grey/RGB/RGBA PNG to (width, height, channels, pixels)."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG file")
    offset, idat, header = 8, bytearray(), None
    while offset < len(data):
        (length,) = struct.unpack(">I", data[offset:offset + 4])
        kind = data[offset + 4:offset + 8]
        body = data[offset + 8:offset + 8 + length]
        if kind == b"IHDR":
            header = struct.unpack(">2I5B", body)
        elif kind == b"IDAT":
            idat.extend(body)
        offset += 12 + length
    if header is None:
        raise ValueError("PNG has no IHDR chunk")
    width, height, depth, colour, _compression, _filter, interlace = header
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(colour)
    if depth != 8 or channels is None or interlace:
        raise ValueError("only non-interlaced 8-bit grey/RGB/RGBA PNGs are supported")
    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    out = bytearray(height * stride)
    previous = bytearray(stride)
    for row in range(height):
        start = row * (stride + 1)
        kind = raw[start]
        line = bytearray(raw[start + 1:start + 1 + stride])
        if kind == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif kind == 2:
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 0xFF
        elif kind == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif kind == 4:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = previous[i]
                c = previous[i - channels] if i >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                predictor = a if pa <= pb and pa <= pc else b if pb <= pc else c
                line[i] = (line[i] + predictor) & 0xFF
        elif kind != 0:
            raise ValueError(f"unknown PNG filter {kind}")
        out[row * stride:(row + 1) * stride] = line
        previous = line
    return width, height, channels, bytes(out)


def png_rgba(width: int, height: int, pixels: bytes) -> bytes:
    """Encode tightly packed RGBA pixels as an 8-bit PNG."""
    stride = width * 4
    raw = b"".join(b"\x00" + pixels[row * stride:(row + 1) * stride] for row in range(height))
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">2I5B", width, height, 8, 6, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(raw, level=6)) + _chunk(b"IEND", b""))


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
