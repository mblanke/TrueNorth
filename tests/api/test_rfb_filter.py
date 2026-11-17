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
VERSION_37 = b"RFB 003.007\n"
CHALLENGE = bytes(range(16))
RESPONSE = bytes(range(100, 116))
RESULT_OK = bytes(4)
SERVER_INIT = struct.pack(">HH", 640, 480) + bytes(16) + struct.pack(">I", 4) + b"mock"


def setenc(*codes: int) -> bytes:
    return bytes([2, 0]) + struct.pack(">H", len(codes)) + b"".join(struct.pack(">i", c) for c in codes)


def cut(text: bytes) -> bytes:
    return bytes([6, 0, 0, 0]) + struct.pack(">I", len(text)) + text


def server_hello(ver: int = 8) -> bytes:
    return b"RFB 003.00%d\n" % ver


def login(f: ViewOnlyFilter, *, ver: int = 8, auth: bool = False, shared: int = 1) -> list[bytes]:
    """A legitimate handshake, the server side driven through ``feed_server`` as a real server
    would speak it (answering each client step): 3.8 or 3.7, security type None or VNC auth.
    Returns what the filter forwarded upstream, in order."""
    sent: list[bytes] = []
    f.feed_server(server_hello(ver))
    sent += f.feed(server_hello(ver))
    f.feed_server(bytes([1, 2 if auth else 1]))
    sent += f.feed(b"\x02" if auth else b"\x01")
    if auth:
        f.feed_server(CHALLENGE)
        sent += f.feed(RESPONSE)
    if auth or ver == 8:
        f.feed_server(RESULT_OK)
    sent += f.feed(bytes([shared]))
    f.feed_server(SERVER_INIT)
    return sent


def ready(ver: int = 8, auth: bool = False) -> ViewOnlyFilter:
    f = ViewOnlyFilter()
    sent = login(f, ver=ver, auth=auth)
    assert sent == [server_hello(ver), b"\x02" if auth else b"\x01", *([RESPONSE] if auth else []), b"\x01"]
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


def at_security(ver: int = 8, offered: bytes = b"\x01") -> ViewOnlyFilter:
    """Server version and list seen, client's version line sent: the choice is next."""
    f = ViewOnlyFilter()
    f.feed_server(server_hello(ver))
    assert f.feed(server_hello(ver)) == [server_hello(ver)]
    f.feed_server(bytes([len(offered)]) + offered)
    return f


@pytest.mark.parametrize("ver, auth", [(8, False), (8, True), (7, False), (7, True)])
def test_the_handshake_passes_in_all_four_flavours_and_clientinit_is_forced_to_shared(ver, auth):
    f = ViewOnlyFilter()
    sent = login(f, ver=ver, auth=auth, shared=0)
    assert sent == [server_hello(ver), b"\x02" if auth else b"\x01", *([RESPONSE] if auth else []), b"\x01"]
    assert f.handshaken
    assert f.feed(KEY) == [] and f.dropped == 1


def test_vnc_authentication_passes_its_16_byte_response_as_opaque_handshake_data():
    f = at_security(8, b"\x02")
    assert f.feed(b"\x02") == [b"\x02"]
    f.feed_server(CHALLENGE)
    response = KEY * 2  # looks like input, is not: it is the auth response, sent after the challenge
    assert f.feed(response) == [response]
    f.feed_server(RESULT_OK)
    assert f.feed(b"\x01") == [b"\x01"]
    f.feed_server(SERVER_INIT)
    assert f.handshaken
    assert f.feed(KEY) == [] and f.dropped == 1


@pytest.mark.parametrize("shared", [0, 1, 7, 255])
def test_clientinit_is_forced_to_shared_so_an_observer_cannot_evict_the_student(shared):
    f = at_security()
    f.feed(b"\x01")
    f.feed_server(RESULT_OK)
    assert f.feed(bytes([shared])) == [b"\x01"]


@pytest.mark.parametrize("version", [b"RFB 003.003\n", b"RFB 003.889\n", b"RFB 004.001\n", b"RFB 003.008 ",
                                     b"rfb 003.008\n", b"\x00" * 12, KEY + POINTER, b"x" * 12])
def test_an_unsupported_or_malformed_version_closes_1008(version):
    f = ViewOnlyFilter()
    f.feed_server(VERSION)
    with pytest.raises(Violation) as e:
        f.feed(version)
    assert e.value.code == 1008


@pytest.mark.parametrize("security", [0, 3, 5, 16, 19, 30, 255])
def test_a_security_type_the_filter_cannot_frame_closes_1008(security):
    f = at_security(8, bytes([1, 2, security]) if security else b"\x01")  # offered, but not framed here
    with pytest.raises(Violation) as e:
        f.feed(bytes([security]))
    assert e.value.code == 1008


def test_input_sent_in_place_of_the_handshake_never_passes():
    # A KeyEvent as the "version": 12 bytes that are not a version string.
    f = ViewOnlyFilter()
    f.feed_server(VERSION)
    with pytest.raises(Violation):
        f.feed(KEY + KEY)
    # The version, then a FramebufferUpdateRequest instead of the security choice: type 3 is not
    # a security type this filter frames, so the connection ends.
    f = at_security()
    with pytest.raises(Violation):
        f.feed(FBUR)
    # A KeyEvent (type 4) in the security slot.
    f = at_security()
    with pytest.raises(Violation):
        f.feed(KEY)
    # A choice, the result, then a KeyEvent where ClientInit goes: its first byte is consumed as
    # the (rewritten) shared flag and the server's ServerInit has not been seen, so the other
    # seven bytes are ahead of the server.
    f = at_security()
    f.feed(b"\x01")
    f.feed_server(RESULT_OK)
    with pytest.raises(Violation):
        f.feed(KEY)


def test_a_partial_version_is_held_not_forwarded():
    f = ViewOnlyFilter()
    f.feed_server(VERSION)
    assert f.feed(VERSION[:11]) == [] and f.held == 11
    assert f.feed(VERSION[11:]) == [VERSION]


def test_messages_sent_before_the_handshake_ends_are_not_messages():
    # ClientInit not yet sent: a FramebufferUpdateRequest here is the shared flag (rewritten),
    # then its 9 remaining bytes are ahead of ServerInit: refused.
    f = at_security()
    f.feed(b"\x01")
    f.feed_server(RESULT_OK)
    with pytest.raises(Violation):
        f.feed(FBUR)


def test_nothing_before_the_handshake_is_forwarded_whole_early():
    f = ViewOnlyFilter()
    f.feed_server(VERSION)
    assert f.feed(b"") == []
    assert f.feed(VERSION) == [VERSION]
    f.feed_server(b"\x01\x02")
    assert f.feed(b"\x02") == [b"\x02"]
    f.feed_server(CHALLENGE)
    assert f.feed(bytes(15)) == [] and f.held == 15  # 15 of 16 auth bytes: held
    assert f.feed(b"\x00") == [bytes(16)]


# ── the server's side of the handshake: the client may not get ahead of it ────

ATTACK = VERSION + b"\x02" + KEY + KEY + b"\x00" + FBUR  # the reviewer's pipelined VNC-auth smuggle


def test_the_pipelined_vnc_auth_attack_closes_with_a_server_that_offered_only_none():
    f = ViewOnlyFilter()
    f.feed_server(VERSION)
    f.feed_server(b"\x01\x01")  # offered: None only
    with pytest.raises(Violation) as e:
        f.feed(ATTACK)
    assert e.value.code == 1008


def test_the_pipelined_vnc_auth_attack_closes_with_a_server_that_offered_vnc_but_sent_no_challenge():
    f = ViewOnlyFilter()
    f.feed_server(VERSION)
    f.feed_server(b"\x01\x02")
    forwarded: list[bytes] = []
    with pytest.raises(Violation) as e:
        forwarded += f.feed(ATTACK)
    assert e.value.code == 1008 and forwarded == []  # nothing, not even the valid prefix, reached the server


def test_the_attack_bytes_in_one_websocket_message_never_reach_upstream_whatever_the_cut():
    for i in range(len(ATTACK) + 1):
        f = ViewOnlyFilter()
        f.feed_server(VERSION)
        f.feed_server(b"\x01\x02")
        sent = b""
        with pytest.raises(Violation):
            sent += b"".join(f.feed(ATTACK[:i]))
            sent += b"".join(f.feed(ATTACK[i:]))
        assert KEY not in sent, i


def test_the_pipelined_vnc_auth_attack_closes_with_a_3_3_server():
    f = ViewOnlyFilter()
    with pytest.raises(Violation) as e:
        f.feed_server(b"RFB 003.003\n")
    assert e.value.code == 1008
    with pytest.raises(Violation):
        f.feed(ATTACK)  # the filter is dead: no byte passes


def test_the_attack_is_refused_even_when_the_challenge_arrives_after_the_extra_bytes():
    f = ViewOnlyFilter()
    f.feed_server(VERSION)
    f.feed_server(b"\x01\x02")
    with pytest.raises(Violation):
        f.feed(VERSION + b"\x02" + KEY)  # 8 bytes of "response" before the challenge exists
    with pytest.raises(Violation):
        f.feed_server(CHALLENGE)  # dead for good


@pytest.mark.parametrize("step", ["version", "list", "challenge", "result", "init"])
def test_a_client_byte_ahead_of_each_server_step_is_a_violation(step):
    f = ViewOnlyFilter()
    ahead = VERSION  # before the server's version
    if step != "version":
        f.feed_server(VERSION)
        ahead = b"\x02"  # the security choice, before the server's list (the version line is fine)
        f.feed(VERSION)
    if step != "list" and step != "version":
        f.feed_server(b"\x01\x02")
        f.feed(b"\x02")
        ahead = RESPONSE  # the auth response, before the challenge
    if step in ("result", "init"):
        f.feed_server(CHALLENGE)
        f.feed(RESPONSE)
        ahead = b"\x01"  # ClientInit, before the SecurityResult
    if step == "init":
        f.feed_server(RESULT_OK)
        f.feed(b"\x01")
        ahead = FBUR  # a message, before ServerInit
    with pytest.raises(Violation) as e:
        f.feed(ahead)
    assert e.value.code == 1008


def test_a_client_version_newer_than_the_servers_is_refused_and_an_older_one_allowed():
    f = ViewOnlyFilter()
    f.feed_server(VERSION_37)
    with pytest.raises(Violation):
        f.feed(VERSION)
    f = ViewOnlyFilter()
    f.feed_server(VERSION)
    assert f.feed(VERSION_37) == [VERSION_37]
    f.feed_server(b"\x01\x01")
    assert f.feed(b"\x01") == [b"\x01"]
    # Answered with 3.7, the server sends no SecurityResult for None, so ClientInit is next.
    assert f.feed(b"\x01") == [b"\x01"]


def test_an_security_type_the_server_did_not_offer_is_refused():
    f = at_security(8, b"\x01")
    with pytest.raises(Violation):
        f.feed(b"\x02")
    f = at_security(8, b"\x02")
    with pytest.raises(Violation):
        f.feed(b"\x01")


def test_clientinit_waits_for_a_zero_security_result_and_a_failed_one_closes():
    f = at_security()
    f.feed(b"\x01")
    with pytest.raises(Violation):
        f.feed(b"\x01")  # 3.8 + None: the SecurityResult has not arrived
    f = at_security()
    f.feed(b"\x01")
    with pytest.raises(Violation) as e:
        f.feed_server(b"\x00\x00\x00\x01")
    assert e.value.code == 1008
    with pytest.raises(Violation):
        f.feed(b"\x01")


@pytest.mark.parametrize("greeting", [b"RFB 003.003\n", b"RFB 003.889\n", b"RFB 004.000\n", b"HTTP/1.1 200 ",
                                      b"\x00" * 12])
def test_a_server_version_other_than_3_7_or_3_8_is_a_violation(greeting):
    f = ViewOnlyFilter()
    with pytest.raises(Violation) as e:
        f.feed_server(greeting)
    assert e.value.code == 1008


def test_a_server_with_no_security_types_is_a_violation():
    f = ViewOnlyFilter()
    f.feed_server(VERSION)
    f.feed(VERSION)
    with pytest.raises(Violation) as e:
        f.feed_server(b"\x00\x00\x00\x00\x07failure")
    assert e.value.code == 1008


def test_a_server_that_offers_no_type_the_filter_frames_ends_the_console_at_the_choice():
    f = at_security(8, bytes([16, 19]))
    for choice in (1, 2, 16, 19):
        g = at_security(8, bytes([16, 19]))
        with pytest.raises(Violation):
            g.feed(bytes([choice]))
    assert f.held == 0


def test_an_oversize_serverinit_name_is_refused_1009_before_the_name_arrives():
    f = at_security()
    f.feed(b"\x01")
    f.feed_server(RESULT_OK)
    f.feed(b"\x01")
    with pytest.raises(Violation) as e:
        f.feed_server(struct.pack(">HH", 1, 1) + bytes(16) + struct.pack(">I", rf.MAX_NAME + 1))
    assert e.value.code == 1009
    f = at_security()
    f.feed(b"\x01")
    f.feed_server(RESULT_OK)
    f.feed(b"\x01")
    f.feed_server(struct.pack(">HH", 1, 1) + bytes(16) + struct.pack(">I", rf.MAX_NAME) + b"n" * rf.MAX_NAME)
    assert f.handshaken  # the bound itself is fine


def test_server_messages_are_framed_as_a_stream_whatever_the_websocket_messages_cut():
    wire = [server_hello(8), b"\x01\x01", RESULT_OK, SERVER_INIT + b"\x00\x00\x00\x00\x00\x00"]  # + a first update
    client = [server_hello(8), b"\x01", b"\x01"]
    for cuts in range(1, 5):
        f = ViewOnlyFilter()
        # one byte per server message, interleaved with the client as a real peer would
        for chunk, answer in zip(wire, [*client, None], strict=True):
            for i in range(0, len(chunk), cuts):
                f.feed_server(chunk[i:i + cuts])
            if answer is not None:
                assert b"".join(f.feed(answer)) in (answer, b"\x01")
        assert f.handshaken
    f = ViewOnlyFilter()  # several server messages merged in one WebSocket message
    f.feed_server(server_hello(8))
    f.feed(server_hello(8))
    f.feed_server(b"\x01\x01" + RESULT_OK[:2])  # the result is early and incomplete: held
    assert f.feed(b"\x01") == [b"\x01"]
    f.feed_server(RESULT_OK[2:])
    assert f.feed(b"\x01") == [b"\x01"]
    f.feed_server(SERVER_INIT[:10])
    assert not f.handshaken
    f.feed_server(SERVER_INIT[10:])
    assert f.handshaken


def test_server_buffering_is_bounded():
    f = ViewOnlyFilter()
    f.feed_server(VERSION)
    f.feed(VERSION)
    f.feed_server(b"\x01\x01")  # waiting for the client's choice now
    with pytest.raises(Violation) as e:
        for _ in range(100):
            f.feed_server(bytes(1000))  # a server that talks ahead for ever
    assert e.value.code == 1009


def test_server_bytes_after_serverinit_are_not_examined_or_held():
    f = ready()
    for _ in range(50):
        f.feed_server(bytes(100_000))
    assert f.held == 0 and f.handshaken
    assert f.feed(FBUR) == [FBUR]


def test_a_violation_from_the_server_side_kills_the_client_side_too():
    f = ViewOnlyFilter()
    with pytest.raises(Violation):
        f.feed_server(b"RFB 003.003\n")
    for _ in range(2):
        with pytest.raises(Violation):
            f.feed(VERSION)
        with pytest.raises(Violation):
            f.feed_server(VERSION)


def test_server_bytes_ahead_of_the_clients_step_are_held_until_it_is_taken():
    f = ViewOnlyFilter()
    f.feed_server(VERSION + b"\x01\x01")  # the list before the client's version line
    assert f.feed(VERSION) == [VERSION]
    assert f.feed(b"\x01") == [b"\x01"]  # the list was kept: the choice is allowed now


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
    # QEMU extended key event (-258) and extended clipboard are offered (and stripped, so the
    # server never enables them), then used anyway (type 255): refused.
    assert f.feed(setenc(0, -258, -1063131698)) == [setenc(0)]
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


# ── SetEncodings: pseudo-encodings that switch on refused client messages are stripped ─────


@pytest.mark.parametrize("code", [-258, -259, -305, -308, -309, -310, -312, -313, -1063131698,
                                  0x574D5600, 0x574D5664, 0x574D56FF])
def test_a_pseudo_encoding_that_enables_a_refused_client_message_is_stripped(code):
    assert ready().feed(setenc(0, code, 1)) == [setenc(0, 1)]


@pytest.mark.parametrize("code", [0, 1, 5, 7, 16, -223, -224, -239, -240, -257, -314, -32, -247, -1,
                                  0x574D55FF, 0x574D5700])
def test_the_encodings_a_viewer_needs_are_kept(code):
    assert ready().feed(setenc(code)) == [setenc(code)]


def test_stripping_keeps_order_and_rewrites_the_count_and_the_next_message_is_still_framed():
    f = ready()
    msg = setenc(-312, 7, -313, 0, -258, 16, -1063131698, -223)
    out = f.feed(msg + KEY + FBUR)
    assert out == [setenc(7, 0, 16, -223), FBUR]
    assert struct.unpack(">H", out[0][2:4]) == (4,) and len(out[0]) == 4 + 4 * 4


def test_a_setencodings_with_everything_stripped_is_forwarded_with_count_zero():
    assert ready().feed(setenc(-312, -313, -258)) == [bytes([2, 0, 0, 0])]


def test_the_padding_byte_is_zeroed_and_the_cap_applies_to_what_the_client_sent():
    assert ready().feed(bytes([2, 0xFF, 0, 1]) + struct.pack(">i", 7)) == [setenc(7)]
    f = ready()
    with pytest.raises(Violation) as e:
        f.feed(setenc(*([-312] * (rf.MAX_ENCODINGS + 1))))  # stripped, but the client sent too many
    assert e.value.code == 1009
    assert len(ready().feed(setenc(*([-312] * rf.MAX_ENCODINGS)))) == 1


def test_a_split_setencodings_is_stripped_the_same():
    msg = setenc(-312, 7, -1063131698, 8)
    for i in range(len(msg) + 1):
        f = ready()
        assert feed_all(f, [msg[:i], msg[i:]]) == setenc(7, 8), i


# ── the handshake, fuzzed against an independent reference server ────────────────────────────

FORBIDDEN_ENCODINGS = {-312, -313, -258, -1063131698}


class RefServer:
    """The hypervisor, as a reference written without the filter's tables. It reads what the
    filter forwarded upstream and fails the test if that is not what an honest client would send
    THIS server: a version no newer than its own, a security type it offered, an auth response
    only after the challenge reached the client, ClientInit only after a zero result, and after
    ServerInit only whole SetPixelFormat / SetEncodings / FramebufferUpdateRequest. It speaks
    only when spoken to; ``sent`` counts what it has produced, and a client unit is only
    acceptable when the filter has been *given* all of it (``delivered``): else the client was
    ahead of the server."""

    def __init__(self, ver: int, offered: list[int], result_ok: bool, name_len: int) -> None:
        self.ver, self.offered, self.result_ok, self.name_len = ver, offered, result_ok, name_len
        self.pending = bytearray(b"RFB 003.00%d\n" % ver)  # produced, not yet given to the filter
        self.sent = len(self.pending)
        self.delivered = 0
        self.rx = bytearray()
        self.stage = "dead" if ver == 3 else "version"  # a 3.3 server: no 3.7/3.8 handshake exists
        self.cver = 0
        self.choice = 0
        self.messages: list[bytes] = []

    def say(self, data: bytes) -> None:
        self.pending += data
        self.sent += len(data)

    def result(self, next_stage: str) -> None:
        self.say(RESULT_OK if self.result_ok else b"\x00\x00\x00\x01")
        self.stage = next_stage if self.result_ok else "dead"

    def receive(self, parts: list[bytes]) -> None:
        for part in parts:
            self.rx += part
            self._run()
            assert not self.rx, "a partial unit reached the server"

    def _need(self, n: int) -> bool:
        if len(self.rx) < n:
            return False
        assert self.delivered >= self.sent, f"the client was ahead of the server at {self.stage}"
        return True

    def _run(self) -> None:
        rx = self.rx
        while rx:
            assert self.stage != "dead", f"bytes reached a server that never allowed them ({bytes(rx[:4])!r})"
            if self.stage == "version":
                if not self._need(12):
                    return
                line = bytes(rx[:12])
                assert line in (b"RFB 003.007\n", b"RFB 003.008\n"), "a bad version line reached the server"
                self.cver = int(line[10:11])
                assert self.cver <= self.ver, "a version newer than the server's reached it"
                del rx[:12]
                if self.offered:
                    self.say(bytes([len(self.offered), *self.offered]))
                    self.stage = "choice"
                else:
                    self.say(b"\x00\x00\x00\x00\x00")  # no types: a reason string follows
                    self.stage = "dead"
            elif self.stage == "choice":
                if not self._need(1):
                    return
                self.choice = rx[0]
                assert self.choice in self.offered, "an un-offered security type reached the server"
                assert self.choice in (1, 2), "a security type the relay cannot frame reached the server"
                del rx[:1]
                if self.choice == 2:
                    self.say(CHALLENGE)
                    self.stage = "auth"
                elif self.cver == 8:
                    self.result("init")
                else:
                    self.stage = "init"
            elif self.stage == "auth":
                if not self._need(16):
                    return
                del rx[:16]  # any 16 bytes: the response, which the server now owes a result for
                self.result("init")
            elif self.stage == "init":
                if not self._need(1):
                    return
                assert rx[0] == 1, "ClientInit was not forced to shared"
                del rx[:1]
                self.say(struct.pack(">HH", 640, 480) + bytes(16) + struct.pack(">I", self.name_len)
                         + b"n" * self.name_len)
                self.stage = "messages"
            else:
                self._message()

    def _message(self) -> None:
        rx = self.rx
        # An independent minimal client-message parser: the server's own reading of the stream.
        assert self.delivered >= self.sent, "a message was sent before ServerInit reached the client"
        kind = rx[0]
        assert kind in (0, 2, 3), f"client message type {kind} (key/pointer/cut/other) reached the server"
        if kind == 2:
            assert len(rx) >= 4, "a truncated SetEncodings reached the server"
            n = 4 + 4 * int.from_bytes(rx[2:4], "big")
            assert len(rx) >= n, "a truncated SetEncodings reached the server"
            for i in range(4, n, 4):
                assert int.from_bytes(rx[i:i + 4], "big", signed=True) not in FORBIDDEN_ENCODINGS
        else:
            n = 20 if kind == 0 else 10
            assert len(rx) >= n, "a truncated message reached the server"
        self.messages.append(bytes(rx[:n]))
        del rx[:n]


def _units(rng: random.Random, srv: RefServer, mutate: bool) -> list[bytes]:
    """What a client would send in order, one entry per protocol step, then (maybe) mutated:
    input bytes smuggled into any slot, units dropped, repeated, swapped, truncated."""
    hello = b"RFB 003.00%d\n" % rng.choice([7, 8])
    pick = rng.choice([c for c in (1, 2) if c in srv.offered] or [rng.choice([1, 2])])
    units = [hello, bytes([pick])]
    if pick == 2:
        units.append(rng.randbytes(16))
    units.append(bytes([rng.randrange(256)]))
    for _ in range(rng.randrange(0, 8)):
        units.append(rng.choice([SETPF, FBUR, setenc(0, 1, -223), setenc(-312, 7, -313), setenc(-1063131698),
                                 KEY, POINTER, cut(b"clip"), cut(b"")]))
    if not mutate:
        return units
    smuggle = [KEY, POINTER, cut(b"AB"), KEY * 2, KEY + POINTER, FBUR, bytes([4]), bytes([255]) + bytes(10),
               b"RFB 003.003\n", b"RFB 003.889\n"]
    for _ in range(rng.randrange(1, 4)):
        i = rng.randrange(len(units) + 1)
        how = rng.randrange(6)
        if how == 0:
            units.insert(i, rng.choice(smuggle))  # input in a handshake slot (or between units)
        elif how == 1 and i < len(units):
            units[i] = rng.choice(smuggle)  # replace a step with input
        elif how == 2 and i < len(units):
            del units[i]  # the step is skipped
        elif how == 3 and i < len(units):
            units.insert(i, units[i])  # repeated
        elif how == 4 and 0 < i < len(units):
            units[i - 1], units[i] = units[i], units[i - 1]  # reordered
        elif i < len(units):
            units[i] = units[i][:rng.randrange(len(units[i]) + 1)]  # truncated
    return units


def _run_case(seed: int) -> tuple[ViewOnlyFilter, RefServer, bool, bool]:
    rng = random.Random(seed)
    ver = rng.choice([8, 8, 8, 7, 7, 3])
    offered = rng.choice([[1], [2], [1, 2], [2, 1], [1], [2], [16], [19, 1], []])
    srv = RefServer(ver, offered, rng.random() < 0.85, rng.choice([0, 4, 100, rf.MAX_NAME, rf.MAX_NAME + 1]))
    mutate, polite = rng.random() < 0.6, rng.random() < 0.5
    units = _units(rng, srv, mutate)
    f = ViewOnlyFilter()
    dead = False

    def drain(limit: int | None = None) -> None:
        nonlocal dead
        while srv.pending and not dead:
            n = rng.randrange(1, len(srv.pending) + 1) if limit is None else min(limit, len(srv.pending))
            chunk = bytes(srv.pending[:n])
            del srv.pending[:n]
            srv.delivered += n
            try:
                f.feed_server(chunk)
            except Violation:
                dead = True

    def send(chunk: bytes) -> None:
        nonlocal dead
        if dead:
            with pytest.raises(Violation):  # every violation kills the filter for good
                f.feed(chunk)
            return
        try:
            parts = f.feed(chunk)
        except Violation:
            dead = True
            return
        srv.receive(parts)

    if polite:  # the client waits for the server between steps (cut inside a step, not across)
        for unit in units:
            drain(rng.choice([None, 1, 3]))
            drain()
            cuts = sorted({rng.randrange(len(unit) + 1) for _ in range(rng.randrange(0, 4))})
            for a, b in zip([0, *cuts], [*cuts, len(unit)], strict=True):
                send(unit[a:b])
    else:  # the client talks whenever it likes, the server answers when it likes
        stream = b"".join(units)
        cuts = sorted({rng.randrange(len(stream) + 1) for _ in range(rng.randrange(0, 10))})
        chunks = [stream[a:b] for a, b in zip([0, *cuts], [*cuts, len(stream)], strict=True)]
        if mutate and chunks and rng.random() < 0.3:
            chunks.insert(rng.randrange(len(chunks)), rng.choice(chunks))  # a chunk sent twice
        for c in chunks:
            if rng.random() < 0.6:
                drain(rng.choice([None, 1, 2, 5]))
            send(c)
    drain()
    if dead:
        with pytest.raises(Violation):
            f.feed_server(b"x")
        with pytest.raises(Violation):
            f.feed(b"")
    return f, srv, mutate, polite


@pytest.mark.parametrize("seed", range(1500))
def test_handshake_fuzz_no_input_ever_reaches_the_server_and_every_violation_is_permanent(seed):
    _run_case(seed)  # RefServer asserts on every byte the filter forwards


def test_handshake_fuzz_covers_what_it_claims():
    """The seeds are not all trivially closed: honest runs complete, and attacks get further than
    the first byte, so the reference had something to judge."""
    completed = attacked = 0
    for seed in range(1500):
        f, srv, mutate, polite = _run_case(seed)
        completed += f.handshaken and srv.stage == "messages"
        attacked += mutate and srv.stage in ("auth", "init", "messages")
    assert completed > 100 and attacked > 50, (completed, attacked)


@pytest.mark.parametrize("seed", range(300))
def test_garbage_from_a_hostile_server_never_makes_the_filter_pass_more_than_framed_messages(seed):
    rng = random.Random(seed)
    f = ViewOnlyFilter()
    dead = False
    for _ in range(rng.randrange(1, 30)):
        try:
            if rng.random() < 0.5:
                f.feed_server(rng.choice([VERSION, VERSION_37, b"\x01\x01", b"\x01\x02", CHALLENGE, RESULT_OK,
                                          SERVER_INIT, rng.randbytes(rng.randrange(0, 50)), bytes(5000)]))
            else:
                f.feed(rng.choice([VERSION, b"\x01", b"\x02", RESPONSE, KEY, FBUR,
                                   rng.randbytes(rng.randrange(0, 30))]))
        except Violation:
            dead = True
            break
        assert f.held <= rf.MAX_FRAMED
    if dead:
        with pytest.raises(Violation):
            f.feed(FBUR)
