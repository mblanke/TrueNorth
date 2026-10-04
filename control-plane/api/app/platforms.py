"""External learning-platform adapters (docs/adr/0001-adapter-registry.md).

What TrueNorth needs to know about each ``external_platforms.platform_type``. Today
that is only how to probe reachability; LMS record exchange goes through ``app/lms``
(xAPI) and ``app/lti13``. Adding a platform = one entry in ``_REGISTRY``; an unknown
type falls back to probing its base URL.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlatformAdapter:
    kind: str
    #: Path appended to ``base_url`` for the reachability probe ("" = the base URL).
    health_path: str = ""

    def health_url(self, base_url: str) -> str:
        return f"{base_url.rstrip('/')}{self.health_path}" if self.health_path else base_url


_REGISTRY: dict[str, PlatformAdapter] = {
    # Answers without a login, unlike /login/token.php.
    "moodle": PlatformAdapter("moodle", "/lib/ajax/service-nologin.php"),
    "immersive_labs": PlatformAdapter("immersive_labs", "/api/health"),
    "offsec": PlatformAdapter("offsec"),
}

_GENERIC = PlatformAdapter("generic")


def get_platform_adapter(kind: str | None) -> PlatformAdapter:
    return _REGISTRY.get(kind or "", _GENERIC)


def supported_platform_types() -> list[str]:
    return sorted(_REGISTRY)
