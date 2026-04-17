"""Circuit breaker for external service calls (Proxmox, Keycloak, OpenSearch).

States: CLOSED → OPEN (after threshold failures) → HALF_OPEN (probe) → CLOSED.
Thread-safe using asyncio locks.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from collections.abc import Callable
from typing import Any, TypeVar

logger = logging.getLogger("truenorth.circuit_breaker")

T = TypeVar("T")


class CircuitState(str, enum.Enum):
    closed = "closed"
    open = "open"
    half_open = "half_open"


class CircuitOpenError(Exception):
    """Raised when a call is rejected because the circuit is open."""

    def __init__(self, name: str, retry_after: float):
        self.name = name
        self.retry_after = retry_after
        super().__init__(f"Circuit '{name}' is OPEN. Retry after {retry_after:.0f}s")


class CircuitBreaker:
    """Async circuit breaker wrapping external service calls.

    Parameters
    ----------
    name : str
        Human-readable name (e.g. "keycloak", "proxmox").
    failure_threshold : int
        Consecutive failures before tripping to OPEN (default: 5).
    recovery_timeout : float
        Seconds to wait in OPEN before allowing a probe (default: 30).
    success_threshold : int
        Consecutive successes in HALF_OPEN to close again (default: 2).
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        success_threshold: int = 2,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.success_threshold = success_threshold

        self._state = CircuitState.closed
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        return self._state

    async def call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Execute *func* through the circuit breaker.

        Raises CircuitOpenError if the circuit is open and recovery
        timeout has not elapsed.
        """
        async with self._lock:
            if self._state == CircuitState.open:
                elapsed = time.monotonic() - self._last_failure_time
                if elapsed < self.recovery_timeout:
                    raise CircuitOpenError(
                        self.name,
                        self.recovery_timeout - elapsed,
                    )
                # Transition to half-open: allow one probe
                self._state = CircuitState.half_open
                self._success_count = 0
                logger.info("Circuit '%s' → HALF_OPEN (probing)", self.name)

        try:
            result = await func(*args, **kwargs)
        except Exception as exc:
            await self._on_failure(exc)
            raise

        await self._on_success()
        return result

    async def _on_failure(self, exc: Exception) -> None:
        async with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()

            if self._state == CircuitState.half_open:
                # Probe failed — back to open
                self._state = CircuitState.open
                logger.warning("Circuit '%s' probe failed → OPEN: %s", self.name, exc)
            elif self._state == CircuitState.closed and self._failure_count >= self.failure_threshold:
                self._state = CircuitState.open
                logger.warning(
                    "Circuit '%s' → OPEN after %d failures: %s",
                    self.name,
                    self._failure_count,
                    exc,
                )

    async def _on_success(self) -> None:
        async with self._lock:
            if self._state == CircuitState.half_open:
                self._success_count += 1
                if self._success_count >= self.success_threshold:
                    self._state = CircuitState.closed
                    self._failure_count = 0
                    logger.info("Circuit '%s' → CLOSED (recovered)", self.name)
            elif self._state == CircuitState.closed:
                # Reset streak on success
                self._failure_count = 0

    def reset(self) -> None:
        """Force-reset the breaker to closed (e.g. for tests)."""
        self._state = CircuitState.closed
        self._failure_count = 0
        self._success_count = 0


# ---------------------------------------------------------------------------
# Singleton breakers for shared external services
# ---------------------------------------------------------------------------
keycloak_breaker = CircuitBreaker("keycloak", failure_threshold=5, recovery_timeout=30)
opensearch_breaker = CircuitBreaker("opensearch", failure_threshold=5, recovery_timeout=15)
proxmox_breaker = CircuitBreaker("proxmox", failure_threshold=3, recovery_timeout=60)
