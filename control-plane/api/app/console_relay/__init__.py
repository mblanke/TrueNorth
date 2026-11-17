"""The console relay: a browser reaches a lab VM's screen only through TrueNorth (S1 of the
console viewer, #366).

``classes/consoles.py`` and ``routers/lab_sessions.py`` authorise a console as before, then
``service.issue`` stores a **grant** and the adapter's ``open`` answers
``{"kind": "rfb-ws", "url": "wss://<tn>/ws/console/<grant>"}``. The browser opens that URL
(``router.py``), signed in as for any WebSocket (``ws_auth``: the ``bearer`` subprotocol), and
the API dials the hypervisor through the range's console adapter and pumps RFB bytes both
ways. See ``router.py`` for the close codes.

Importing this package registers its table on ``Base.metadata``.
"""

from .models import ConsoleGrant

__all__ = ["ConsoleGrant"]
