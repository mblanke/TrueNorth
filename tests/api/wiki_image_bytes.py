"""Smallest valid-enough image files for the wiki image tests (no imaging library needed)."""

import struct
import zlib

GPS = b"GPS 51.5N 0.12W Canon EOS secret-camera-serial"


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def png(w=2, h=2, *, metadata=True) -> bytes:
    raw = b"".join(b"\x00" + b"\x80\x80\x80" * w for _ in range(h))
    out = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    if metadata:
        out += (
            _chunk(b"tEXt", b"Comment\x00" + GPS)
            + _chunk(b"eXIf", GPS)
            + _chunk(b"tIME", b"\x07\xea\x0b\x04\x09\x00\x00")
        )
    return out + _chunk(b"IDAT", zlib.compress(raw)) + _chunk(b"IEND", b"")


def jpeg(w=3, h=2, *, metadata=True) -> bytes:
    def seg(marker, body):
        return b"\xff" + bytes([marker]) + struct.pack(">H", len(body) + 2) + body

    out = b"\xff\xd8" + seg(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
    if metadata:
        out += seg(0xE1, b"Exif\x00\x00" + GPS) + seg(0xED, b"Photoshop 3.0\x00" + GPS) + seg(0xFE, GPS)
    out += seg(0xC0, struct.pack(">BHHB", 8, h, w, 1) + b"\x01\x11\x00")
    out += seg(0xDA, b"\x01\x01\x00\x00\x3f\x00") + b"\x12\x34\x56\xff\x00\x78" + b"\xff\xd9"
    return out


def gif(w=2, h=2) -> bytes:
    return b"GIF89a" + struct.pack("<HH", w, h) + b"\x00\x00\x00" + b"\x3b"


def webp(w=4, h=3) -> bytes:
    body = (
        b"WEBP"
        + b"VP8X"
        + struct.pack("<I", 10)
        + b"\x00\x00\x00\x00"
        + (w - 1).to_bytes(3, "little")
        + (h - 1).to_bytes(3, "little")
    )
    return b"RIFF" + struct.pack("<I", len(body)) + body


def webp_lossless(w=5, h=7) -> bytes:
    bits = (w - 1) | ((h - 1) << 14)
    chunk = b"\x2f" + bits.to_bytes(4, "little") + b"\x00" * 12
    body = b"WEBP" + b"VP8L" + struct.pack("<I", len(chunk)) + chunk
    return b"RIFF" + struct.pack("<I", len(body)) + body


SVG = b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><script>alert(1)</script></svg>'


def seg(marker: int, body: bytes) -> bytes:
    return b"\xff" + bytes([marker]) + struct.pack(">H", len(body) + 2) + body


def exif_with_orientation(value: int) -> bytes:
    tiff = b"MM\x00*\x00\x00\x00\x08" + struct.pack(">H", 2)
    tiff += struct.pack(">HHIHH", 0x0112, 3, 1, value, 0)
    tiff += struct.pack(">HHI", 0x8825, 4, 1) + struct.pack(">I", 0x2000)  # a GPS IFD pointer
    return seg(0xE1, b"Exif\x00\x00" + tiff + b"\x00\x00\x00\x00" + GPS)


def jpeg_hostile() -> bytes:
    """Exif before and between scans, an MPF second picture, ICC, and a payload after EOI."""
    sof = seg(0xC0, struct.pack(">BHHB", 8, 2, 3, 1) + b"\x01\x11\x00")
    sos = seg(0xDA, b"\x01\x01\x00\x00\x3f\x00")
    return (
        b"\xff\xd8"
        + seg(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
        + exif_with_orientation(6)
        + seg(0xE2, b"MPF\x00" + GPS)
        + seg(0xE2, b"ICC_PROFILE\x00\x01\x01profile")
        + sof
        + sos
        + b"\x12\x34\xff\x00\x56"
        + seg(0xE1, b"Exif\x00\x00AFTER-SCAN " + GPS)
        + seg(0xFE, GPS)
        + sos
        + b"\x78\x9a"
        + b"\xff\xd9"
        + b"<script>alert(1)</script>"
        + GPS
    )


def gif_hostile() -> bytes:
    comment = b"\x21\xfe" + bytes([len(GPS)]) + GPS + b"\x00"
    xmp = b"\x21\xff\x0bXMP DataXMP" + b"\x03abc\x00"
    loop = b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00"
    frame = b"\x2c" + struct.pack("<HHHH", 0, 0, 2, 2) + b"\x00" + b"\x02" + b"\x02\x44\x01\x00"
    return (
        b"GIF89a"
        + struct.pack("<HH", 2, 2)
        + b"\x00\x00\x00"
        + loop
        + comment
        + xmp
        + frame
        + b"\x3b"
        + b"<script>alert(1)</script>"
    )


def webp_hostile() -> bytes:
    def chunk(tag, body):
        return tag + struct.pack("<I", len(body)) + body + (b"\x00" if len(body) % 2 else b"")

    vp8x = chunk(b"VP8X", bytes([0x0C]) + b"\x00\x00\x00" + (3).to_bytes(3, "little") + (2).to_bytes(3, "little"))
    body = b"WEBP" + vp8x + chunk(b"EXIF", GPS) + chunk(b"XMP ", GPS) + chunk(b"ICCP", b"profile!")
    return b"RIFF" + struct.pack("<I", len(body)) + body + b"<script>alert(1)</script>"
