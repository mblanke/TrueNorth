"""The view-only allowlist: which browser-to-hypervisor RFB messages a view-only grant may send (#369).

A console is RFB over a WebSocket (WebMKS). A WebSocket message is not an RFB message: a
browser, or a hostile client, may split one RFB message over several WebSocket messages and
join several in one. So this filter works on the *byte stream*, not on WebSocket messages: it
frames the stream itself, exactly as the hypervisor will, and forwards only whole messages it
has parsed and approved. What reaches the hypervisor is therefore a sequence of complete
allowed messages; no byte the filter did not frame and approve is ever forwarded, so the
hypervisor cannot read a different message boundary than the filter did.

The filter also *observes the server's side* of the handshake (``feed_server``: every
hypervisor-to-browser WebSocket message, which the relay still forwards unchanged). The client
may not get ahead of the server: the 16 bytes after a VNC-auth choice are opaque to this filter,
so they are let through only once the server has really sent the challenge, and every other
client step likewise waits for the server item before it. A client byte that arrives early is a
violation (1008), so a client cannot smuggle input into a slot (the auth response, say) that the
server never opened. The server's side, in order (RFC 6143)::

    ProtocolVersion   12 bytes   3.7 or 3.8 only (3.3, and anything else, is a violation)
    (client's version line)
    security types    count, then the types; a count of 0 (failure reason follows) is refused
    (client's choice; for type 2:)  challenge 16 bytes
    SecurityResult     4 bytes   3.8 always; 3.7 only for type 2; non-zero is refused
    (client's ClientInit)
    ServerInit        24 bytes + a name (4-byte length, at most ``MAX_NAME`` or 1009)

After ServerInit the server's bytes are no longer examined. Server bytes are buffered (at most
``MAX_SERVER_BUF``) only while they wait for the client's next step.

The client side of RFB 3.7/3.8, in order (RFC 6143)::

    ProtocolVersion   12 bytes   ``RFB 003.007\\n`` or ``RFB 003.008\\n``, no higher than the
                                 server's, sent only after the server's version was seen
    security choice    1 byte    1 (None) or 2 (VNC authentication), a type the server offered;
                                 any other type has a handshake this filter cannot frame
    VNC auth response 16 bytes   only after choice 2 and the server's challenge
    ClientInit         1 byte    only after the security phase succeeded. The shared flag is
                                 FORCED to 1 (shared): a view-only watcher must
                                 never be able to ask for an exclusive session, which makes a
                                 VNC server disconnect the other viewers, i.e. the Student
    then messages, only after ServerInit was seen, each ``type`` byte and a body:

    ==== ======================= ==========================================  =============
    type message                 length                                      view-only
    ==== ======================= ==========================================  =============
    0    SetPixelFormat          20                                          forwarded
    2    SetEncodings            4 + 4*n  (n = u16 at offset 2)              forwarded, minus
                                                                             ``STRIPPED``
    3    FramebufferUpdateRequest 10                                         forwarded
    4    KeyEvent                8                                           dropped
    5    PointerEvent            6                                           dropped
    6    ClientCutText           8 + length (u32 at offset 4)                dropped
    else (anything, incl. 1, 7-255: SetDesktopSize 251, QEMU/VMware extensions 255/127,
          EnableContinuousUpdates 150 ...)                                   connection closed
    ==== ======================= ==========================================  =============

It is an allowlist: a type is forwarded only if it is in the first three rows. The types
whose length is known to be fixed by the RFB spec (4, 5, 6) are consumed exactly and dropped,
so the stream stays in step and the connection survives; the viewer sees no effect. Any other
type has a length this filter does not know, so it cannot skip it and cannot resynchronise:
the connection is closed (1008). An extension that adds a client message type (QEMU extended key
event, extended clipboard, SetDesktopSize) is just an unknown type here and closes the socket;
so that an honest viewer does not get the socket closed by the server asking it to use one
(Fence, ContinuousUpdates, extended clipboard ...), the pseudo-encodings that switch such
extensions on are removed from the SetEncodings list before it is forwarded (count rewritten).

Bounds and what happens when the input is not well-formed:

* Buffering is bounded. Only the unfinished tail of one allowed message is held, at most
  ``MAX_FRAMED`` bytes (a SetEncodings of ``MAX_ENCODINGS`` entries); ClientCutText bodies are
  counted off and discarded, never buffered. The caller bounds each WebSocket message
  (``router.MAX_CLIENT_MESSAGE``).
* A SetEncodings with more than ``MAX_ENCODINGS`` entries, or a ClientCutText longer than
  ``MAX_CUT_TEXT``, closes the socket with 1009 (too big). The length fields are therefore
  never trusted to skip: a 4 GiB cut text cannot silence the rest of the stream.
* A *partial* message at the end of a WebSocket message is held until the rest arrives and
  is forwarded (or dropped) whole; if the socket closes first it is never forwarded. A
  client that stalls mid-message is closed by the relay's idle timer.
* A malformed handshake (wrong version string, unknown, un-offered or unsupported security
  type, a client step ahead of the server, a server that refuses or is not 3.7/3.8) closes
  the socket with 1008. After any ``Violation`` the filter is dead and refuses all input.
* The filter fails closed: a hypervisor whose handshake deviates from RFC 6143 3.7/3.8 gets its
  console closed, never its input passed.
* Text WebSocket frames carry no RFB; the relay refuses them (1003) before they get here.
"""

from __future__ import annotations

import struct

#: Close codes the relay sends for a violation (RFC 6455 section 7.4.1).
CLOSE_POLICY, CLOSE_TOO_BIG = 1008, 1009

VERSIONS = (b"RFB 003.007\n", b"RFB 003.008\n")
#: Security types whose client side is framed here: 1 None (nothing more), 2 VNC auth (16 bytes).
SECURITY_NONE, SECURITY_VNC = 1, 2
VNC_AUTH_RESPONSE = 16

SET_PIXEL_FORMAT, SET_ENCODINGS, FRAMEBUFFER_UPDATE_REQUEST = 0, 2, 3
KEY_EVENT, POINTER_EVENT, CLIENT_CUT_TEXT = 4, 5, 6
_FORWARD_LEN = {SET_PIXEL_FORMAT: 20, FRAMEBUFFER_UPDATE_REQUEST: 10}
_DROP_LEN = {KEY_EVENT: 8, POINTER_EVENT: 6}

#: Real viewers offer about 20 encodings; the u16 field would allow 65535 (256 KiB).
MAX_ENCODINGS = 1024
#: A view-only client has no business sending clipboard text; a generous bound all the same.
MAX_CUT_TEXT = 1024 * 1024
#: The longest message held while unfinished.
MAX_FRAMED = 4 + 4 * MAX_ENCODINGS

#: ServerInit's desktop name: real ones are a few dozen bytes.
MAX_NAME = 4096
#: Server bytes held while they wait for the client's next step (ServerInit is at most 24 + MAX_NAME).
MAX_SERVER_BUF = 24 + MAX_NAME + 64

#: Pseudo-encodings removed from a forwarded SetEncodings. Each switches on a message the server
#: then expects the client to send (or answer), none of which a view-only console passes:
#: QEMU extended key event -258 and audio -259, GII -305, ExtendedDesktopSize -308 (the client
#: then sends SetDesktopSize), Xvp -309, OLIVE call control -310, Fence -312 (the client must
#: answer), ContinuousUpdates -313, Extended Clipboard 0xC0A1E5CE (the client answers with a
#: ClientCutText of negative length). The VMware range 0x574D56xx carries VMware's own client
#: message (type 127) enablers. The desktop resizes through plain DesktopSize -223 instead.
STRIPPED = frozenset({-258, -259, -305, -308, -309, -310, -312, -313, -1063131698})
STRIPPED_RANGES = ((0x574D5600, 0x574D56FF),)

_VERSION, _SECURITY, _AUTH, _INIT, _MESSAGES = range(5)
#: Where the server's side of the handshake is: its version seen and waiting for the client's
#: version line, the security list, waiting for the client's choice, the challenge, the result,
#: waiting for ClientInit, ServerInit, and done (not examined any more).
(_SV_VERSION, _SV_VERSION_WAIT, _SV_LIST, _SV_CHOICE_WAIT, _SV_CHALLENGE, _SV_RESULT,
 _SV_INIT_WAIT, _SV_INIT, _SV_DONE) = range(9)


def _stripped(code: int) -> bool:
    return code in STRIPPED or any(lo <= code <= hi for lo, hi in STRIPPED_RANGES)


class Violation(Exception):  # noqa: N818 — a protocol violation, reported by close code
    """The client's bytes are not something a view-only console passes. ``reason`` is safe to
    show the Student; it never contains client bytes."""

    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code, self.reason = code, reason


class ViewOnlyFilter:
    """Feed it the client's bytes as they arrive (``feed``: it returns the messages to forward)
    and the server's (``feed_server``: observed only, the relay forwards them as they are)."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._state = _VERSION
        self._skip = 0  # bytes of a dropped message still to discard
        self._dead = False
        self.dropped = 0  # input messages dropped (key, pointer, cut text)
        # The server's side.
        self._sbuf = bytearray()
        self._sstate = _SV_VERSION
        self._sver = 0  # the server's minor version, 7 or 8
        self._cver = 0  # the version the client answered with
        self._offered: frozenset[int] | None = None  # None until the security list was seen
        self._choice = 0
        self._challenge_seen = False
        self._security_done = False
        self._serverinit_seen = False

    # ── the server's side ────────────────────────────────────────────────────

    def feed_server(self, data: bytes) -> None:
        """Observe bytes the hypervisor sent. Raises ``Violation`` if its handshake is not one this
        filter can vouch for (the relay closes the console with the violation's code)."""
        if self._dead:
            raise Violation(CLOSE_POLICY, "Input is not allowed on a view-only console")
        if self._sstate == _SV_DONE:
            return
        try:
            self._sbuf += data
            self._advance_server()
            if len(self._sbuf) > MAX_SERVER_BUF:
                raise Violation(CLOSE_TOO_BIG, "The machine's console sent more than a handshake allows")
        except BaseException:
            self._dead = True
            raise

    def _advance_server(self) -> None:
        """Parse every server item that is complete and that the client's progress allows."""
        sbuf = self._sbuf
        while True:
            state = self._sstate
            if state == _SV_VERSION:
                if len(sbuf) < 12:
                    return
                version = bytes(sbuf[:12])
                if version not in VERSIONS:
                    raise Violation(CLOSE_POLICY, "This console speaks a protocol version that is not supported")
                self._sver = VERSIONS.index(version) + 7
                del sbuf[:12]
                self._sstate = _SV_VERSION_WAIT
            elif state == _SV_LIST:
                if not sbuf:
                    return
                count = sbuf[0]
                if count == 0:
                    raise Violation(CLOSE_POLICY, "The machine refused the console connection")
                if len(sbuf) < 1 + count:
                    return
                self._offered = frozenset(sbuf[1:1 + count])
                del sbuf[:1 + count]
                self._sstate = _SV_CHOICE_WAIT
            elif state == _SV_CHALLENGE:
                if len(sbuf) < VNC_AUTH_RESPONSE:
                    return
                del sbuf[:VNC_AUTH_RESPONSE]
                self._challenge_seen = True
                self._sstate = _SV_RESULT
            elif state == _SV_RESULT:
                if len(sbuf) < 4:
                    return
                if bytes(sbuf[:4]) != b"\x00\x00\x00\x00":
                    raise Violation(CLOSE_POLICY, "The machine refused the console connection")
                del sbuf[:4]
                self._security_done = True
                self._sstate = _SV_INIT_WAIT
            elif state == _SV_INIT:
                if len(sbuf) < 24:
                    return
                (name_len,) = struct.unpack_from(">I", sbuf, 20)
                if name_len > MAX_NAME:
                    raise Violation(CLOSE_TOO_BIG, "The machine's console announced an oversize name")
                if len(sbuf) < 24 + name_len:
                    return
                self._serverinit_seen = True
                self._sstate = _SV_DONE
                sbuf.clear()  # what follows is the framebuffer stream: not examined
                return
            else:  # waiting for the client's version line, choice or ClientInit; or done
                return

    @property
    def held(self) -> int:
        """Bytes of an unfinished message being held."""
        return len(self._buf)

    @property
    def handshaken(self) -> bool:
        return self._state == _MESSAGES and self._serverinit_seen

    def feed(self, data: bytes) -> list[bytes]:
        """The whole messages in ``data`` (with what was held) that may go to the hypervisor,
        in order, one entry per RFB message (handshake units included). Raises ``Violation``."""
        if self._dead:
            raise Violation(CLOSE_POLICY, "Input is not allowed on a view-only console")
        try:
            return self._run(data)
        except BaseException:
            self._dead = True  # fail closed: nothing more passes after any error
            raise

    def _run(self, data: bytes) -> list[bytes]:
        self._buf += data
        out: list[bytes] = []
        buf = self._buf
        while True:
            if self._skip:
                n = min(self._skip, len(buf))
                del buf[:n]
                self._skip -= n
                if self._skip:
                    return out  # buf is empty: the rest of a dropped message comes next
                continue
            take = self._next(out)
            if take == 0:
                return out

    def _next(self, out: list[bytes]) -> int:
        """Handle one unit at the head of the buffer; returns bytes consumed (0: need more)."""
        buf, state = self._buf, self._state
        if not buf:
            return 0
        # A client byte is only ever judged against what the server has already said: if the
        # server item that opens this step has not been seen, the client is ahead of it.
        if state == _VERSION:
            self._require(self._sstate >= _SV_VERSION_WAIT)
            if len(buf) < 12:
                return 0
            version = bytes(buf[:12])
            if version not in VERSIONS:
                raise Violation(CLOSE_POLICY, "This console speaks a protocol version that is not supported")
            self._cver = VERSIONS.index(version) + 7
            if self._cver > self._sver:
                raise Violation(CLOSE_POLICY, "That protocol version is newer than the console's")
            self._sstate = _SV_LIST
            n = self._pass(out, 12, _SECURITY)
            self._advance_server()
            return n
        if state == _SECURITY:
            self._require(self._offered is not None)
            choice = buf[0]
            if choice not in (SECURITY_NONE, SECURITY_VNC) or choice not in self._offered:
                raise Violation(CLOSE_POLICY, "This console's security type is not supported")
            self._choice = choice
            if choice == SECURITY_VNC:
                self._sstate = _SV_CHALLENGE
            elif self._cver >= 8:
                self._sstate = _SV_RESULT  # 3.8 answers None with a SecurityResult too
            else:  # 3.7 and None: the security phase is over with the choice
                self._security_done = True
                self._sstate = _SV_INIT_WAIT
            n = self._pass(out, 1, _AUTH if choice == SECURITY_VNC else _INIT)
            self._advance_server()
            return n
        if state == _AUTH:
            self._require(self._challenge_seen)
            if len(buf) < VNC_AUTH_RESPONSE:
                return 0
            return self._pass(out, VNC_AUTH_RESPONSE, _INIT)
        if state == _INIT:
            self._require(self._security_done)
            del buf[:1]
            out.append(b"\x01")  # the shared flag, whatever the client sent
            self._state = _MESSAGES
            self._sstate = _SV_INIT
            self._advance_server()
            return 1
        self._require(self._serverinit_seen)
        return self._message(out)

    @staticmethod
    def _require(allowed: bool) -> None:
        if not allowed:
            raise Violation(CLOSE_POLICY, "The console is not ready for that yet")

    def _pass(self, out: list[bytes], n: int, next_state: int) -> int:
        out.append(bytes(self._buf[:n]))
        del self._buf[:n]
        self._state = next_state
        return n

    def _message(self, out: list[bytes]) -> int:
        buf = self._buf
        if not buf:
            return 0
        kind = buf[0]
        if kind in _FORWARD_LEN:
            n = _FORWARD_LEN[kind]
        elif kind == SET_ENCODINGS:
            if len(buf) < 4:
                return 0
            (count,) = struct.unpack_from(">H", buf, 2)
            if count > MAX_ENCODINGS:
                raise Violation(CLOSE_TOO_BIG, "Too many encodings offered")
            n = 4 + 4 * count
            if len(buf) < n:
                return 0
            codes = [c for c in struct.unpack_from(f">{count}i", buf, 4) if not _stripped(c)]
            out.append(struct.pack(">BxH", SET_ENCODINGS, len(codes)) + struct.pack(f">{len(codes)}i", *codes))
            del buf[:n]
            return n
        elif kind in _DROP_LEN:
            self._skip = _DROP_LEN[kind]
            self.dropped += 1
            return 1  # progress; the skip loop discards the message
        elif kind == CLIENT_CUT_TEXT:
            if len(buf) < 8:
                return 0
            (length,) = struct.unpack_from(">I", buf, 4)
            if length > MAX_CUT_TEXT:
                raise Violation(CLOSE_TOO_BIG, "Clipboard text is too large")
            self._skip = 8 + length
            self.dropped += 1
            return 1
        else:
            raise Violation(CLOSE_POLICY, "That input is not allowed on a view-only console")
        if len(buf) < n:
            return 0
        out.append(bytes(buf[:n]))
        del buf[:n]
        return n
