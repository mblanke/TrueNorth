"""The view-only allowlist: which browser-to-hypervisor RFB messages a view-only grant may send (#369).

A console is RFB over a WebSocket (WebMKS). A WebSocket message is not an RFB message: a
browser, or a hostile client, may split one RFB message over several WebSocket messages and
join several in one. So this filter works on the *byte stream*, not on WebSocket messages: it
frames the stream itself, exactly as the hypervisor will, and forwards only whole messages it
has parsed and approved. What reaches the hypervisor is therefore a sequence of complete
allowed messages; no byte the filter did not frame and approve is ever forwarded, so the
hypervisor cannot read a different message boundary than the filter did.

The client side of RFB 3.7/3.8, in order (RFC 6143)::

    ProtocolVersion   12 bytes   ``RFB 003.007\\n`` or ``RFB 003.008\\n`` (3.3 is refused: there the
                                 *server* picks the security type, which this filter, reading
                                 only the client's bytes, could not know; and 3.889 etc. likewise)
    security choice    1 byte    1 (None) or 2 (VNC authentication); any other type has a
                                 handshake this filter cannot frame, so it is refused
    VNC auth response 16 bytes   only after choice 2
    ClientInit         1 byte    the shared flag. FORCED to 1 (shared): a view-only watcher must
                                 never be able to ask for an exclusive session, which makes a
                                 VNC server disconnect the other viewers, i.e. the Student
    then messages, each ``type`` byte and a body:

    ==== ======================= ==========================================  =============
    type message                 length                                      view-only
    ==== ======================= ==========================================  =============
    0    SetPixelFormat          20                                          forwarded
    2    SetEncodings            4 + 4*n  (n = u16 at offset 2)              forwarded
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
the connection is closed (1008). Which pseudo-encodings a client offered in SetEncodings does
not matter: an extension that adds a client message type (QEMU extended key event, extended
clipboard, SetDesktopSize) is just an unknown type here and closes the socket.

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
* A malformed handshake (wrong version string, unknown or unsupported security type) closes
  the socket with 1008. After any ``Violation`` the filter is dead and refuses all input.
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

_VERSION, _SECURITY, _AUTH, _INIT, _MESSAGES = range(5)


class Violation(Exception):  # noqa: N818 — a protocol violation, reported by close code
    """The client's bytes are not something a view-only console passes. ``reason`` is safe to
    show the Student; it never contains client bytes."""

    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code, self.reason = code, reason


class ViewOnlyFilter:
    """Feed it the client's bytes as they arrive; it returns the messages to forward."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._state = _VERSION
        self._skip = 0  # bytes of a dropped message still to discard
        self._dead = False
        self.dropped = 0  # input messages dropped (key, pointer, cut text)

    @property
    def held(self) -> int:
        """Bytes of an unfinished message being held."""
        return len(self._buf)

    @property
    def handshaken(self) -> bool:
        return self._state == _MESSAGES

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
        if state == _VERSION:
            if len(buf) < 12:
                return 0
            if bytes(buf[:12]) not in VERSIONS:
                raise Violation(CLOSE_POLICY, "This console speaks a protocol version that is not supported")
            return self._pass(out, 12, _SECURITY)
        if state == _SECURITY:
            if not buf:
                return 0
            if buf[0] not in (SECURITY_NONE, SECURITY_VNC):
                raise Violation(CLOSE_POLICY, "This console's security type is not supported")
            return self._pass(out, 1, _AUTH if buf[0] == SECURITY_VNC else _INIT)
        if state == _AUTH:
            if len(buf) < VNC_AUTH_RESPONSE:
                return 0
            return self._pass(out, VNC_AUTH_RESPONSE, _INIT)
        if state == _INIT:
            if not buf:
                return 0
            del buf[:1]
            out.append(b"\x01")  # the shared flag, whatever the client sent
            self._state = _MESSAGES
            return 1
        return self._message(out)

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
