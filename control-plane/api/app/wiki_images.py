"""Check and clean an image uploaded to a wiki page (issue #379).

The type of an upload is decided from its **bytes**, never from the filename or the
``Content-Type`` the browser sent: only PNG, JPEG, GIF and WebP pass. SVG is refused (it is
a script carrier, and served from this origin it would run as the signed-in user). The
dimensions come from the header, and an image of more than ``MAX_PIXELS`` is refused (a small
file can decode to gigabytes in the reader's browser).

``clean`` removes metadata that a screenshot or photo carries without the author knowing:
JPEG Exif/XMP/IPTC/comment segments (camera, GPS, editing software), and PNG ``eXIf`` /
text / time chunks. GIF and WebP are stored as received; their metadata is rare. Nothing is
re-encoded, so pixels are never changed; a file that cannot be walked cleanly is refused.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

MAX_BYTES = 5 * 1024 * 1024
MAX_PIXELS = 40_000_000
MAX_SIDE = 16_384

TYPES = {"png": "image/png", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
EXTENSIONS = {"png": "png", "jpeg": "jpg", "gif": "gif", "webp": "webp"}


class ImageError(ValueError):
    """The upload is not an acceptable image; the message is safe to show the uploader."""


@dataclass(frozen=True)
class Checked:
    kind: str  # png | jpeg | gif | webp
    width: int
    height: int
    data: bytes  # cleaned

    @property
    def content_type(self) -> str:
        return TYPES[self.kind]


def _png(data: bytes) -> tuple[int, int]:
    if len(data) < 33 or data[12:16] != b"IHDR":
        raise ImageError("That PNG is damaged.")
    return struct.unpack(">II", data[16:24])


def _gif(data: bytes) -> tuple[int, int]:
    if len(data) < 10:
        raise ImageError("That GIF is damaged.")
    return struct.unpack("<HH", data[6:10])


def _jpeg_segments(data: bytes):
    """Yield (marker, start, end) for each segment up to and including the start of scan."""
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            raise ImageError("That JPEG is damaged.")
        marker = data[pos + 1]
        if marker == 0xFF:  # fill byte
            pos += 1
            continue
        if marker in (0x01, *range(0xD0, 0xD8)):  # standalone markers carry no length
            yield marker, pos, pos + 2
            pos += 2
            continue
        (length,) = struct.unpack(">H", data[pos + 2 : pos + 4])
        end = pos + 2 + length
        if length < 2 or end > len(data):
            raise ImageError("That JPEG is damaged.")
        yield marker, pos, end
        if marker == 0xDA:  # start of scan: entropy-coded data follows to the end
            return
        pos = end
    raise ImageError("That JPEG is damaged.")


def _jpeg_size(data: bytes) -> tuple[int, int]:
    for marker, start, end in _jpeg_segments(data):
        if marker in range(0xC0, 0xD0) and marker not in (0xC4, 0xC8, 0xCC):
            if end - start < 9:
                raise ImageError("That JPEG is damaged.")
            h, w = struct.unpack(">HH", data[start + 5 : start + 9])
            return w, h
    raise ImageError("That JPEG is damaged.")


def _webp(data: bytes) -> tuple[int, int]:
    if len(data) < 30:
        raise ImageError("That WebP is damaged.")
    form = data[12:16]
    if form == b"VP8 ":
        if data[23:26] != b"\x9d\x01\x2a":
            raise ImageError("That WebP is damaged.")
        w, h = struct.unpack("<HH", data[26:30])
        return w & 0x3FFF, h & 0x3FFF
    if form == b"VP8L":
        if data[20] != 0x2F:
            raise ImageError("That WebP is damaged.")
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if form == b"VP8X":
        return int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1
    raise ImageError("That WebP is damaged.")


def _kind(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    head = data[:512].lstrip().lower()
    if head.startswith((b"<svg", b"<?xml", b"<!doctype", b"<html")):
        raise ImageError("SVG and HTML are not accepted. Upload a PNG, JPEG, GIF or WebP.")
    raise ImageError("That is not a PNG, JPEG, GIF or WebP image.")


_JPEG_DROP = {0xE1, 0xED, 0xFE}  # APP1 (Exif, XMP), APP13 (IPTC/Photoshop), COM
_PNG_DROP = {b"eXIf", b"tEXt", b"iTXt", b"zTXt", b"tIME"}


def _clean_jpeg(data: bytes) -> bytes:
    out = bytearray(b"\xff\xd8")
    last = 2
    for marker, start, end in _jpeg_segments(data):
        if marker in _JPEG_DROP:
            out += data[last:start]
            last = end
    out += data[last:]
    return bytes(out)


def _clean_png(data: bytes) -> bytes:
    out = bytearray(data[:8])
    pos = 8
    seen_end = False
    while pos < len(data):
        if pos + 12 > len(data):
            raise ImageError("That PNG is damaged.")
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        end = pos + 12 + length
        if end > len(data):
            raise ImageError("That PNG is damaged.")
        if data[pos + 4 : pos + 8] not in _PNG_DROP:
            out += data[pos:end]
        if data[pos + 4 : pos + 8] == b"IEND":
            seen_end = True
            break
        pos = end
    if not seen_end:
        raise ImageError("That PNG is damaged.")
    return bytes(out)


def check(data: bytes) -> Checked:
    """Validate and clean one upload, or raise ImageError."""
    if not data:
        raise ImageError("The file is empty.")
    if len(data) > MAX_BYTES:
        raise ImageError(f"Images can be at most {MAX_BYTES // (1024 * 1024)} MB.")
    kind = _kind(data)
    width, height = {"png": _png, "jpeg": _jpeg_size, "gif": _gif, "webp": _webp}[kind](data)
    if width < 1 or height < 1:
        raise ImageError(f"That {kind.upper()} is damaged.")
    if width > MAX_SIDE or height > MAX_SIDE or width * height > MAX_PIXELS:
        raise ImageError("That image is too large in pixels. Resize it and try again.")
    if kind == "jpeg":
        data = _clean_jpeg(data)
    elif kind == "png":
        data = _clean_png(data)
    return Checked(kind=kind, width=width, height=height, data=data)
