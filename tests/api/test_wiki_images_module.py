"""app/wiki_images.py: what is accepted, what is refused, what is removed (#379)."""

import contextlib
import struct

import pytest
from app import wiki_images as wi

from tests.api import wiki_image_bytes as b


@pytest.mark.parametrize(
    ("data", "kind", "size"),
    [
        (b.png(3, 2), "png", (3, 2)),
        (b.jpeg(7, 5), "jpeg", (7, 5)),
        (b.gif(4, 6), "gif", (4, 6)),
        (b.webp(9, 8), "webp", (9, 8)),
        (b.webp_lossless(5, 7), "webp", (5, 7)),
    ],
)
def test_the_type_and_size_come_from_the_bytes(data, kind, size):
    got = wi.check(data)
    assert (got.kind, (got.width, got.height), got.content_type) == (kind, size, wi.TYPES[kind])


@pytest.mark.parametrize(
    "data",
    [b.SVG, b"<!DOCTYPE html><script>alert(1)</script>", b"%PDF-1.7", b"MZ\x90\x00", b"plain text", b"\x00" * 64],
)
def test_other_types_are_refused_whatever_the_filename_claims(data):
    with pytest.raises(wi.ImageError):
        wi.check(data)


def test_empty_and_oversized_are_refused():
    with pytest.raises(wi.ImageError, match="empty"):
        wi.check(b"")
    with pytest.raises(wi.ImageError, match="at most"):
        wi.check(b.png() + b"\x00" * wi.MAX_BYTES)


def test_a_huge_canvas_in_a_tiny_file_is_refused():
    with pytest.raises(wi.ImageError, match="too large"):
        wi.check(b.png(20_000, 20_000))
    with pytest.raises(wi.ImageError, match="too large"):
        wi.check(b.gif(7_000, 7_000))  # under the side limit, over the pixel limit


@pytest.mark.parametrize("cut", [10, 20, 40])
def test_truncated_files_are_refused_not_crashed_on(cut):
    for data in (b.png(), b.jpeg(), b.gif(), b.webp()):
        with contextlib.suppress(wi.ImageError):  # refused is fine; any other exception is the bug
            wi.check(data[:cut])


def test_jpeg_metadata_is_removed_and_the_picture_is_untouched():
    src = b.jpeg()
    assert b.GPS in src
    got = wi.check(src)
    assert b.GPS not in got.data and b"Exif" not in got.data and b"Photoshop" not in got.data
    assert got.data.startswith(b"\xff\xd8") and got.data.endswith(b"\x12\x34\x56\xff\x00\x78\xff\xd9")
    assert got.data == b.jpeg(metadata=False)
    assert wi.check(got.data).data == got.data  # cleaning twice changes nothing


def test_png_metadata_is_removed_and_the_pixels_are_untouched():
    src = b.png()
    assert b.GPS in src
    got = wi.check(src)
    assert b.GPS not in got.data
    assert got.data == b.png(metadata=False)


def test_a_png_without_iend_or_with_a_bad_chunk_length_is_refused():
    with pytest.raises(wi.ImageError):
        wi.check(b.png()[:-12])
    bad = bytearray(b.png())
    bad[33:37] = struct.pack(">I", 10_000_000)
    with pytest.raises(wi.ImageError):
        wi.check(bytes(bad))
