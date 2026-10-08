"""Parsing YAML someone else wrote, without a billion laughs.

``yaml.safe_load`` refuses tags that build objects, but it still expands anchors and
aliases: a few hundred bytes of nested aliases become gigabytes once the result is walked
or serialised (the "billion laughs"). ``NoAliasLoader`` refuses any alias, and ``load``
also caps the input size. Content in this platform (scenarios, templates, run files)
never needs aliases.

Shared by ``routers/arc2_studio.py`` (run files) and ``engine_bridge.validate_yaml``
(the scenario/template validation endpoints), security sweep M4.
"""

from __future__ import annotations

from typing import Any

import yaml

MAX_YAML_BYTES = 1024 * 1024


class NoAliasLoader(yaml.SafeLoader):
    """SafeLoader without anchors/aliases."""

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise yaml.YAMLError("YAML aliases are not accepted")
        return super().compose_node(parent, index)


class YamlTooLargeError(yaml.YAMLError):
    """The document is over the size cap."""


def load(text: str | bytes, max_bytes: int = MAX_YAML_BYTES) -> Any:
    """Parse ``text`` with ``NoAliasLoader``. Raises ``yaml.YAMLError`` (``YamlTooLargeError``
    over ``max_bytes``)."""
    size = len(text.encode("utf-8")) if isinstance(text, str) else len(text)
    if size > max_bytes:
        raise YamlTooLargeError(f"document is larger than {max_bytes} bytes")
    return yaml.load(text, Loader=NoAliasLoader)  # noqa: S506 - SafeLoader subclass
