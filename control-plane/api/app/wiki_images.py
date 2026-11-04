"""Check and clean an image uploaded to a wiki page (issue #379).

The type of an upload is decided from its **bytes**, never from the filename or the
``Content-Type`` the browser sent: only PNG, JPEG, GIF and WebP pass. SVG is refused (it is
a script carrier, and served from this origin it would run as the signed-in user). The
dimensions come from the header, and an image of more than ``MAX_PIXELS`` is refused (a small
file can decode to gigabytes in the reader's browser).

Metadata a screenshot or photo carries without the author knowing is removed, and so is
anything after the end of the picture (a second image, a video, a script):

* JPEG: every segment is walked, through all scans, and only the picture's own are kept (JFIF,
  ICC profile, Adobe colour transform, tables, frames, scans). Exif, XMP, IPTC, comments and
  the multi-picture (MPF) block go. The Exif **orientation** is the one thing kept, in a new
  minimal Exif segment, because without it a portrait phone photo shows sideways.
* PNG: ``eXIf``, text and time chunks, and anything after IEND.
* GIF: comments, plain-text and application extensions other than the loop count; anything
  after the trailer. Frame count is capped.
* WebP: ``EXIF`` and ``XMP `` chunks (and their flags), anything after the RIFF payload.

Nothing is re-encoded, so pixels never change; a file that cannot be walked cleanly is refused.
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


_PNG_DROP = {b"eXIf", b"tEXt", b"iTXt", b"zTXt", b"tIME"}


MAX_GIF_FRAMES = 500


def _jpeg_walk(data: bytes):
    """Yield ('seg', marker, bytes) and ('scan', None, bytes) through EOI; ImageError if it is not whole."""
    n = len(data)
    pos = 2
    while pos < n:
        if data[pos] != 0xFF:
            raise ImageError("That JPEG is damaged.")
        while pos + 1 < n and data[pos + 1] == 0xFF:
            pos += 1  # fill bytes
        if pos + 1 >= n:
            break
        marker = data[pos + 1]
        if marker == 0xD9:  # EOI: the picture ends here, whatever follows
            return
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            yield "seg", marker, data[pos : pos + 2]
            pos += 2
            continue
        if marker == 0x00 or pos + 4 > n:
            raise ImageError("That JPEG is damaged.")
        (length,) = struct.unpack(">H", data[pos + 2 : pos + 4])
        end = pos + 2 + length
        if length < 2 or end > n:
            raise ImageError("That JPEG is damaged.")
        yield "seg", marker, data[pos:end]
        pos = end
        if marker == 0xDA:  # entropy-coded data runs to the next real marker
            start = pos
            while pos + 1 < n and not (
                data[pos] == 0xFF and data[pos + 1] not in (0x00, 0xFF) and not 0xD0 <= data[pos + 1] <= 0xD7
            ):
                pos += 1
            if pos + 1 >= n:
                raise ImageError("That JPEG is damaged.")
            yield "scan", None, data[start:pos]
    raise ImageError("That JPEG is damaged.")


def _jpeg_keep(marker: int, seg: bytes) -> bool:
    if marker == 0xFE or marker in range(0xE1, 0xF0) and marker not in (0xE2, 0xEE):
        return False  # comments, Exif/XMP (APP1), IPTC (APP13) and the rest of the APPn
    if marker == 0xE2:
        return seg[4:15] == b"ICC_PROFILE"  # not the MPF second-picture block
    if marker == 0xEE:
        return seg[4:9] == b"Adobe"
    return True


def _exif_orientation(data: bytes) -> int:
    """The Exif orientation (1-8) of a JPEG, or 1 if there is none or it cannot be read."""
    try:
        for kind, marker, seg in _jpeg_walk(data):
            if kind == "seg" and marker == 0xE1 and seg[4:10] == b"Exif\x00\x00":
                tiff = seg[10:]
                order = ">" if tiff[:2] == b"MM" else "<" if tiff[:2] == b"II" else ""
                if not order:
                    return 1
                (ifd,) = struct.unpack(order + "I", tiff[4:8])
                (count,) = struct.unpack(order + "H", tiff[ifd : ifd + 2])
                for i in range(min(count, 64)):
                    at = ifd + 2 + 12 * i
                    tag, typ, cnt = struct.unpack(order + "HHI", tiff[at : at + 8])
                    if tag == 0x0112 and typ == 3 and cnt == 1:
                        (val,) = struct.unpack(order + "H", tiff[at + 8 : at + 10])
                        return val if 1 <= val <= 8 else 1
    except (struct.error, ImageError, IndexError):
        return 1
    return 1


def _orientation_segment(value: int) -> bytes:
    tiff = (
        b"MM\x00*\x00\x00\x00\x08"
        + struct.pack(">H", 1)
        + struct.pack(">HHIHH", 0x0112, 3, 1, value, 0)
        + b"\x00\x00\x00\x00"
    )
    body = b"Exif\x00\x00" + tiff
    return b"\xff\xe1" + struct.pack(">H", len(body) + 2) + body


def _jpeg_size(data: bytes) -> tuple[int, int]:
    for kind, marker, seg in _jpeg_walk(data):
        if kind == "seg" and marker in range(0xC0, 0xD0) and marker not in (0xC4, 0xC8, 0xCC):
            if len(seg) < 9:
                raise ImageError("That JPEG is damaged.")
            h, w = struct.unpack(">HH", seg[5:9])
            return w, h
    raise ImageError("That JPEG is damaged.")


def _clean_jpeg(data: bytes) -> bytes:
    orientation = _exif_orientation(data)
    out = bytearray(b"\xff\xd8")
    placed = orientation == 1
    for kind, marker, part in _jpeg_walk(data):
        if kind == "scan":
            out += part
        elif _jpeg_keep(marker, part):
            if not placed and marker != 0xE0:
                out += _orientation_segment(orientation)  # after JFIF, before the tables and frame
                placed = True
            out += part
    out += b"\xff\xd9"
    return bytes(out)


def _gif_blocks(data: bytes):
    """Walk a GIF; yield (kind, bytes). ImageError unless it ends in a trailer."""
    n = len(data)
    if n < 13:
        raise ImageError("That GIF is damaged.")
    pos = 13
    if data[10] & 0x80:
        pos += 3 * (2 << (data[10] & 7))
    yield "head", data[:pos]
    frames = 0

    def subblocks(at: int) -> int:
        while True:
            if at >= n:
                raise ImageError("That GIF is damaged.")
            size = data[at]
            at += 1 + size
            if size == 0:
                return at

    while pos < n:
        kind = data[pos]
        if kind == 0x3B:
            yield "tail", b"\x3b"
            return
        if kind == 0x21:
            if pos + 2 > n:
                raise ImageError("That GIF is damaged.")
            end = subblocks(pos + 2)
            label = data[pos + 1]
            keep = label == 0xF9 or (label == 0xFF and data[pos + 3 : pos + 14] == b"NETSCAPE2.0")
            yield ("keep" if keep else "drop"), data[pos:end]
            pos = end
        elif kind == 0x2C:
            frames += 1
            if frames > MAX_GIF_FRAMES:
                raise ImageError("That GIF has too many frames.")
            if pos + 10 > n:
                raise ImageError("That GIF is damaged.")
            after = pos + 10
            if data[pos + 9] & 0x80:
                after += 3 * (2 << (data[pos + 9] & 7))
            if after >= n:
                raise ImageError("That GIF is damaged.")
            end = subblocks(after + 1)
            yield "keep", data[pos:end]
            pos = end
        else:
            raise ImageError("That GIF is damaged.")
    raise ImageError("That GIF is damaged.")


def _clean_gif(data: bytes) -> bytes:
    return b"".join(part for kind, part in _gif_blocks(data) if kind != "drop")


def _webp_chunks(data: bytes):
    """Yield (fourcc, chunk bytes incl. header and pad) through the RIFF payload."""
    if len(data) < 12:
        raise ImageError("That WebP is damaged.")
    (size,) = struct.unpack("<I", data[4:8])
    end = 8 + size
    if end > len(data) or size < 4:
        raise ImageError("That WebP is damaged.")
    pos = 12
    while pos < end:
        if pos + 8 > end:
            raise ImageError("That WebP is damaged.")
        (length,) = struct.unpack("<I", data[pos + 4 : pos + 8])
        stop = pos + 8 + length + (length & 1)
        if pos + 8 + length > end:
            raise ImageError("That WebP is damaged.")
        yield data[pos : pos + 4], data[pos : min(stop, end)]
        pos = stop


def _clean_webp(data: bytes) -> bytes:
    body = bytearray(b"WEBP")
    for fourcc, chunk in _webp_chunks(data):
        if fourcc in (b"EXIF", b"XMP "):
            continue
        if fourcc == b"VP8X":
            chunk = chunk[:8] + bytes([chunk[8] & ~0x0C & 0xFF]) + chunk[9:]  # no EXIF / XMP flags
        body += chunk if len(chunk) % 2 == 0 else chunk + b"\x00"
    return b"RIFF" + struct.pack("<I", len(body)) + bytes(body)


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
    data = {"jpeg": _clean_jpeg, "png": _clean_png, "gif": _clean_gif, "webp": _clean_webp}[kind](data)
    return Checked(kind=kind, width=width, height=height, data=data)
