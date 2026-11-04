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


class TestReviewFindings:
    """The adversarial review of #379: each of these was a real gap."""

    def test_a_photos_orientation_survives_so_it_does_not_show_sideways(self):
        got = wi.check(b.jpeg_hostile()).data
        assert wi._exif_orientation(got) == 6
        assert b.GPS not in got and b"GPS" not in got  # the pointer and the camera data are gone

    def test_exif_after_the_first_scan_trailing_bytes_and_the_second_picture_are_gone(self):
        got = wi.check(b.jpeg_hostile()).data
        for needle in (b.GPS, b"AFTER-SCAN", b"<script>", b"MPF"):
            assert needle not in got
        assert got.endswith(b"\xff\xd9") and got.count(b"\xff\xd9") == 1
        assert b"ICC_PROFILE" in got and got.count(b"\xff\xda") == 2  # the picture's own parts stay

    def test_the_cleaned_photo_is_still_a_walkable_jpeg_of_the_same_size(self):
        got = wi.check(wi.check(b.jpeg_hostile()).data)
        assert (got.width, got.height) == (3, 2)

    def test_an_orientation_of_one_adds_no_exif_at_all(self):
        src = b.jpeg(metadata=False)
        assert wi.check(src).data == src

    def test_gif_comments_extensions_and_trailing_bytes_go_but_the_loop_and_frame_stay(self):
        got = wi.check(b.gif_hostile()).data
        for needle in (b.GPS, b"XMP Data", b"<script>"):
            assert needle not in got
        assert b"NETSCAPE2.0" in got and got.endswith(b"\x3b") and b"\x2c" in got

    def test_gif_with_too_many_frames_or_no_trailer_is_refused(self):
        frame = b"\x2c" + b"\x00" * 8 + b"\x00" + b"\x02\x01\x00\x00"
        head = b"GIF89a" + struct.pack("<HH", 2, 2) + b"\x00\x00\x00"
        with pytest.raises(wi.ImageError, match="too many"):
            wi.check(head + frame * (wi.MAX_GIF_FRAMES + 1) + b"\x3b")
        with pytest.raises(wi.ImageError):
            wi.check(head + frame)

    def test_webp_exif_xmp_and_trailing_bytes_go_and_the_profile_stays(self):
        got = wi.check(b.webp_hostile()).data
        for needle in (b.GPS, b"EXIF", b"XMP ", b"<script>"):
            assert needle not in got
        assert b"ICCP" in got
        assert struct.unpack("<I", got[4:8])[0] == len(got) - 8
        assert got[20] & 0x0C == 0  # the VP8X flags no longer promise Exif or XMP
        assert wi.check(got).data == got

    def test_a_webp_that_lies_about_its_size_is_refused(self):
        bad = bytearray(b.webp())
        bad[4:8] = struct.pack("<I", 10_000)
        with pytest.raises(wi.ImageError):
            wi.check(bytes(bad))
