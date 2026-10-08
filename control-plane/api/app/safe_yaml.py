"""Parsing YAML someone else wrote, without a billion laughs.

``yaml.safe_load`` refuses tags that build objects, but it still expands anchors and
aliases: a few hundred bytes of nested aliases become gigabytes once the result is walked
or serialised (the "billion laughs"). ``NoAliasLoader`` refuses any alias, and ``load``
also caps the input size. Content in this platform (scenarios, templates, run files)
never needs aliases.

Every YAML parse in ``app/`` goes through ``load`` (security sweep M4);
``tests/api/test_safe_yaml_everywhere.py`` fails on a direct ``yaml.safe_load``/``yaml.load``.
Both refusals subclass ``yaml.YAMLError``, so a caller's existing ``except yaml.YAMLError``
still applies; a write path that tolerates unparseable YAML catches ``YamlRefusedError``
to refuse these anyway.
"""

from __future__ import annotations

from typing import Any

import yaml

MAX_YAML_BYTES = 1024 * 1024


class YamlRefusedError(yaml.YAMLError):
    """Refused by policy (an alias, or over the size cap) rather than a syntax error."""


class YamlTooLargeError(YamlRefusedError):
    """The document is over the size cap."""


class NoAliasLoader(yaml.SafeLoader):
    """SafeLoader without anchors/aliases."""

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise YamlRefusedError("YAML aliases are not accepted")
        return super().compose_node(parent, index)


def load(text: str | bytes, max_bytes: int = MAX_YAML_BYTES) -> Any:
    """Parse ``text`` with ``NoAliasLoader``. Raises ``yaml.YAMLError``: ``YamlRefusedError``
    for an alias, ``YamlTooLargeError`` (a ``YamlRefusedError``) over ``max_bytes``."""
    size = len(text.encode("utf-8")) if isinstance(text, str) else len(text)
    if size > max_bytes:
        raise YamlTooLargeError(f"document is larger than {max_bytes} bytes")
    return yaml.load(text, Loader=NoAliasLoader)  # noqa: S506 - SafeLoader subclass
