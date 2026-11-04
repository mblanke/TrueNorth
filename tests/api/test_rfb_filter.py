"""The view-only allowlist for a console's RFB input (app/console_relay/rfb_filter.py, #369).

Pure byte-stream tests, no sockets. The aim is that no KeyEvent (4), PointerEvent (5) or
ClientCutText (6) reaches the hypervisor however the client cuts, joins, pads or mislabels its
bytes. ``frame`` below is an independent reference parser of the *forwarded* stream, as the
hypervisor would read it: the invariant is that it sees only SetPixelFormat, SetEncodings and
FramebufferUpdateRequest, each whole, from the first byte to the last.
"""

from __future__ import annotations

import random
import struct

import pytest
from app.console_relay import rfb_filter as rf
from app.console_relay.rfb_filter import ViewOnlyFilter, Violation

VERSION = b"RFB 003.008\n"
SETPF = bytes([0, 0, 0, 0]) + bytes(16)
FBUR = bytes([3, 1]) + struct.pack(">HHHH", 0, 0, 640, 480)
KEY = bytes([4, 1, 0, 0]) + struct.pack(">I", 0xFF0D)
POINTER = bytes([5, 1]) + struct.pack(">HH", 10, 20)
LOGIN = VERSION + b"\x01" + b"\x01"


def setenc(*codes: int) -> bytes:
    return bytes([2, 0]) + struct.pack(">H", len(codes)) + b"".join(struct.pack(">i", c) for c in codes)


def cut(text: bytes) -> bytes:
    return bytes([6, 0, 0, 0]) + struct.pack(">I", len(text)) + text


def ready() -> ViewOnlyFilter:
    f = ViewOnlyFilter()
    assert f.feed(LOGIN) == [VERSION, b"\x01", b"\x01"]
    assert f.handshaken
    return f


def feed_all(f: ViewOnlyFilter, chunks: list[bytes]) -> bytes:
    return b"".join(b"".join(f.feed(c)) for c in chunks)


def frame(stream: bytes) -> list[tuple[int, bytes]]:
    """The hypervisor's reading of a post-handshake client stream; raises on a type outside the
    allowlist or a truncated message."""
    out, i = [], 0
    while i < len(stream):
        kind = stream[i]
        if kind == 0:
            n = 20
        elif kind == 3:
            n = 10
        elif kind == 2:
            n = 4 + 4 * struct.unpack_from(">H", stream, i + 2)[0]
        else:
            raise AssertionError(f"type {kind} reached the hypervisor")
        assert i + n <= len(stream), "a truncated message reached the hypervisor"
        out.append((kind, stream[i:i + n]))
        i += n
    return out


# ── the handshake ────────────────────────────────────────────────────────────


def test_the_handshake_passes_with_a_none_security_type():
    f = ViewOnlyFilter()
    assert f.feed(VERSION) == [VERSION]
    assert f.feed(b"\x01") == [b"\x01"]
    assert f.feed(b"\x01") == [b"\x01"] and f.handshaken


def test_vnc_authentication_passes_its_16_byte_response_as_opaque_handshake_data():
    f = ViewOnlyFilter()
    f.feed(VERSION)
    assert f.feed(b"\x02") == [b"\x02"]
    response = KEY * 2  # looks like input, is not: it is the auth response
    assert f.feed(response) == [response]
    assert f.feed(b"\x01") == [b"\x01"] and f.handshaken
    assert f.feed(KEY) == [] and f.dropped == 1


@pytest.mark.parametrize("shared", [0, 1, 7, 255])
def test_clientinit_is_forced_to_shared_so_an_observer_cannot_evict_the_student(shared):
    f = ViewOnlyFilter()
    f.feed(VERSION + b"\x01")
    assert f.feed(bytes([shared])) == [b"\x01"]


@pytest.mark.parametrize("version", [b"RFB 003.003\n", b"RFB 003.889\n", b"RFB 004.001\n", b"RFB 003.008 ",
                                     b"rfb 003.008\n", b"\x00" * 12, KEY + POINTER, b"x" * 12])
def test_an_unsupported_or_malformed_version_closes_1008(version):
    with pytest.raises(Violation) as e:
        ViewOnlyFilter().feed(version)
    assert e.value.code == 1008


@pytest.mark.parametrize("security", [0, 3, 5, 16, 19, 30, 255])
def test_a_security_type_the_filter_cannot_frame_closes_1008(security):
    f = ViewOnlyFilter()
    f.feed(VERSION)
    with pytest.raises(Violation) as e:
        f.feed(bytes([security]))
    assert e.value.code == 1008


def test_input_sent_in_place_of_the_handshake_never_passes():
    # A KeyEvent as the "version": 12 bytes that are not a version string.
    with pytest.raises(Violation):
        ViewOnlyFilter().feed(KEY + KEY)
    # The version, then a FramebufferUpdateRequest instead of the security choice: type 3 is not
    # a security type this filter frames, so the connection ends.
    f = ViewOnlyFilter()
    f.feed(VERSION)
    with pytest.raises(Violation):
        f.feed(FBUR)
    # A KeyEvent (type 4) in the security slot.
    f = ViewOnlyFilter()
    f.feed(VERSION)
    with pytest.raises(Violation):
        f.feed(KEY)
    # Version and a security choice, then a KeyEvent where ClientInit goes: its first byte is
    # consumed as the (rewritten) shared flag, the other seven begin with 0x01, an unknown type.
    f = ViewOnlyFilter()
    f.feed(VERSION + b"\x01")
    with pytest.raises(Violation):
        f.feed(KEY)


def test_a_partial_version_is_held_not_forwarded():
    f = ViewOnlyFilter()
    assert f.feed(VERSION[:11]) == [] and f.held == 11
    assert f.feed(VERSION[11:]) == [VERSION]


def test_messages_sent_before_the_handshake_ends_are_not_messages():
    # ClientInit not yet sent: a FramebufferUpdateRequest here is the shared flag (rewritten),
    # then its 9 remaining bytes begin a message of type 1: refused.
    f = ViewOnlyFilter()
    f.feed(VERSION + b"\x01")
    with pytest.raises(Violation):
        f.feed(FBUR)


def test_nothing_before_the_handshake_is_forwarded_whole_early():
    f = ViewOnlyFilter()
    assert f.feed(b"") == []
    assert f.feed(VERSION + b"\x02" + bytes(15)) == [VERSION, b"\x02"]  # 15 of 16 auth bytes: held
    assert f.held == 15
    assert f.feed(b"\x00" + b"\x00") == [bytes(16), b"\x01"]


# ── the allowlist ────────────────────────────────────────────────────────────


def test_the_three_allowed_messages_are_forwarded_whole_and_in_order():
    f = ready()
    stream = SETPF + setenc(0, 1, -223) + FBUR + setenc() + FBUR
    assert f.feed(stream) == [SETPF, setenc(0, 1, -223), FBUR, setenc(), FBUR]


def test_key_pointer_and_cut_text_are_dropped_and_the_stream_stays_in_step():
    f = ready()
    stream = KEY + FBUR + POINTER + cut(b"hello") + FBUR + cut(b"") + KEY
    assert f.feed(stream) == [FBUR, FBUR]
    assert f.dropped == 5 and f.held == 0


@pytest.mark.parametrize("kind", [k for k in range(256) if k not in (0, 2, 3, 4, 5, 6)])
def test_every_other_message_type_closes_1008(kind):
    f = ready()
    with pytest.raises(Violation) as e:
        f.feed(bytes([kind]) + bytes(40))
    assert e.value.code == 1008


def test_after_a_violation_nothing_more_passes_and_the_chunks_valid_part_is_not_sent():
    f = ready()
    with pytest.raises(Violation):
        f.feed(FBUR + bytes([251]) + bytes(8))  # SetDesktopSize: unknown length to us
    with pytest.raises(Violation):
        f.feed(FBUR)
    with pytest.raises(Violation):
        f.feed(b"")


def test_extension_messages_enabled_by_pseudo_encodings_are_still_refused():
    f = ready()
    # QEMU extended key event (-258) and extended clipboard are offered, then used (type 255).
    assert f.feed(setenc(0, -258, -1063131698)) == [setenc(0, -258, -1063131698)]
    with pytest.raises(Violation):
        f.feed(bytes([255, 0, 0, 1]) + bytes(10))
    f = ready()
    with pytest.raises(Violation):
        f.feed(bytes([127, 1]) + bytes(10))  # VMware's client message type


# ── message splitting ────────────────────────────────────────────────────────

STREAM = SETPF + KEY + setenc(0, 1) + POINTER + FBUR + cut(b"clip") + FBUR + KEY
EXPECTED = SETPF + setenc(0, 1) + FBUR + FBUR


def test_a_stream_cut_at_every_single_position_forwards_the_same_bytes():
    for i in range(len(STREAM) + 1):
        f = ready()
        assert feed_all(f, [STREAM[:i], STREAM[i:]]) == EXPECTED, i
        assert f.held == 0


def test_a_stream_fed_one_byte_at_a_time_forwards_the_same_bytes():
    f = ready()
    assert feed_all(f, [STREAM[i:i + 1] for i in range(len(STREAM))]) == EXPECTED


def test_a_stream_cut_in_three_at_every_pair_of_positions_forwards_the_same_bytes():
    for i in range(0, len(STREAM) + 1, 3):
        for j in range(i, len(STREAM) + 1, 5):
            f = ready()
            assert feed_all(f, [STREAM[:i], STREAM[i:j], STREAM[j:]]) == EXPECTED, (i, j)


def test_one_websocket_message_holding_many_rfb_messages_is_framed_individually():
    f = ready()
    assert f.feed(KEY * 50 + FBUR + POINTER * 50 + FBUR) == [FBUR, FBUR]


def test_a_keyevent_split_across_messages_is_dropped_whole_not_half_forwarded():
    f = ready()
    assert f.feed(KEY[:3]) == [] and f.feed(KEY[3:5]) == [] and f.feed(KEY[5:] + FBUR) == [FBUR]


def test_a_keyevent_started_inside_a_partial_allowed_message_is_that_messages_body():
    # The attacker ends one chunk mid-FramebufferUpdateRequest and starts the next with a
    # KeyEvent, hoping the next chunk's first byte is read as a message start. It is not: the
    # filter reads the stream, so those bytes are the FBUR's body (and forwarded as such, as the
    # hypervisor reads them). What follows is judged from there: here, an unknown type.
    f = ready()
    assert f.feed(FBUR[:4]) == []
    with pytest.raises(Violation):
        f.feed(KEY + FBUR)
    # With a body that completes cleanly the hypervisor sees one FBUR and no key.
    f = ready()
    f.feed(FBUR[:4])
    out = f.feed(KEY[:6] + FBUR)
    assert out == [FBUR[:4] + KEY[:6], FBUR]


# ── length fields ────────────────────────────────────────────────────────────


def test_setencodings_forwards_exactly_the_bytes_its_count_covers():
    f = ready()
    # Count 1 followed by bytes shaped like a KeyEvent: the count covers one encoding (4 bytes).
    # The rest is read as the next message exactly as the hypervisor reads it: here a
    # SetPixelFormat (type 0) still waiting for its 20 bytes, so nothing more is forwarded.
    lying = bytes([2, 0, 0, 1]) + KEY + FBUR
    assert f.feed(lying) == [lying[:8]]
    assert f.held == 14


def test_a_key_event_whose_body_is_inside_a_setencodings_entry_is_just_an_encoding():
    f = ready()
    msg = bytes([2, 0, 0, 2]) + KEY
    assert f.feed(msg) == [msg]  # 8 bytes of "encodings": the hypervisor reads them as such too


def test_setencodings_with_zero_entries_is_four_bytes_and_the_next_message_starts_after_it():
    f = ready()
    assert f.feed(bytes([2, 0, 0, 0]) + KEY + FBUR) == [bytes([2, 0, 0, 0]), FBUR]


def test_too_many_encodings_closes_1009():
    f = ready()
    with pytest.raises(Violation) as e:
        f.feed(bytes([2, 0]) + struct.pack(">H", rf.MAX_ENCODINGS + 1))
    assert e.value.code == 1009
    f = ready()
    assert len(f.feed(setenc(*range(rf.MAX_ENCODINGS)))) == 1  # the bound itself is allowed


def test_the_count_is_judged_before_the_body_arrives_so_a_huge_one_buffers_nothing():
    f = ready()
    with pytest.raises(Violation):
        f.feed(bytes([2, 0, 0xFF, 0xFF]))  # 65535 entries announced, none sent
    assert f.held <= 4


def test_cut_text_is_counted_off_and_discarded_without_buffering():
    f = ready()
    stream = cut(b"A" * 100_000) + FBUR
    out = []
    for i in range(0, len(stream), 1000):
        out += f.feed(stream[i:i + 1000])
        assert f.held <= 1000
    assert out == [FBUR]


def test_cut_text_whose_body_looks_like_input_is_discarded_with_the_body():
    f = ready()
    assert f.feed(cut(KEY + POINTER + FBUR) + FBUR) == [FBUR]


def test_cut_text_length_past_the_bound_closes_1009_it_is_never_skipped():
    for length in (rf.MAX_CUT_TEXT + 1, 0x7FFFFFFF, 0xFFFFFFFF):
        f = ready()
        with pytest.raises(Violation) as e:
            f.feed(bytes([6, 0, 0, 0]) + struct.pack(">I", length))
        assert e.value.code == 1009
    f = ready()
    assert f.feed(cut(b"x" * rf.MAX_CUT_TEXT) + FBUR) == [FBUR]  # the bound itself is dropped, not refused


def test_a_cut_text_header_split_in_the_length_field_is_still_judged():
    f = ready()
    assert f.feed(bytes([6, 0, 0, 0, 0xFF, 0xFF])) == []
    with pytest.raises(Violation):
        f.feed(bytes([0xFF, 0xFF]))


def test_a_pointer_event_followed_by_cut_text_with_a_huge_length_in_one_chunk():
    f = ready()
    with pytest.raises(Violation):
        f.feed(POINTER + bytes([6, 0, 0, 0, 0xFF, 0xFF, 0xFF, 0xFF]))


# ── bounded buffering ────────────────────────────────────────────────────────


def test_held_bytes_never_exceed_the_longest_allowed_message():
    f = ready()
    biggest = setenc(*range(rf.MAX_ENCODINGS))
    for b in biggest[:-1]:
        f.feed(bytes([b]))
        assert f.held <= rf.MAX_FRAMED
    assert f.feed(biggest[-1:]) == [biggest] and f.held == 0


def test_a_partial_message_is_held_and_forwarded_only_once_whole():
    f = ready()
    assert f.feed(FBUR[:9]) == [] and f.held == 9
    assert f.feed(FBUR[9:]) == [FBUR] and f.held == 0


# ── the invariant, fuzzed ────────────────────────────────────────────────────


def _random_stream(rng: random.Random) -> bytes:
    parts = []
    for _ in range(rng.randrange(1, 40)):
        pick = rng.randrange(9)
        if pick == 0:
            parts.append(SETPF)
        elif pick == 1:
            parts.append(FBUR)
        elif pick == 2:
            parts.append(setenc(*[rng.randrange(-400, 20) for _ in range(rng.randrange(0, 6))]))
        elif pick == 3:
            parts.append(KEY)
        elif pick == 4:
            parts.append(POINTER)
        elif pick == 5:
            parts.append(cut(rng.randbytes(rng.randrange(0, 30))))
        elif pick == 6:  # a lying encoding count: what follows does not match it
            parts.append(bytes([2, 0, 0, rng.randrange(0, 4)]) + rng.randbytes(rng.randrange(0, 20)))
        elif pick == 7:  # raw noise
            parts.append(rng.randbytes(rng.randrange(1, 12)))
        else:
            parts.append(KEY[:rng.randrange(1, 8)])  # truncated input
    return b"".join(parts)


@pytest.mark.parametrize("seed", range(300))
def test_whatever_a_client_sends_and_however_it_is_cut_the_hypervisor_reads_only_allowed_messages(seed):
    rng = random.Random(seed)
    stream = _random_stream(rng)
    cuts = sorted(rng.sample(range(len(stream) + 1), min(len(stream) + 1, rng.randrange(0, 8))))
    chunks = [stream[a:b] for a, b in zip([0, *cuts], [*cuts, len(stream)], strict=True)]
    f = ready()
    sent = b""
    try:
        for c in chunks:
            sent += b"".join(f.feed(c))
    except Violation:
        pass
    # Framed by the reference parser, what was forwarded is whole allowed messages only: no
    # KeyEvent, PointerEvent or ClientCutText at a message boundary, no truncated message.
    try:
        frame(sent)
    except AssertionError as exc:
        pytest.fail(f"seed {seed}: {exc}")
    # The same bytes cut elsewhere: the same forwarded bytes unless the whole is refused.
    whole = ready()
    try:
        expected = b"".join(whole.feed(stream))
    except Violation:
        return
    again = ready()
    assert b"".join(b"".join(again.feed(c)) for c in chunks) == expected, f"seed {seed}"
