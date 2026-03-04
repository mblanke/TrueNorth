"""TrueNorth Range — Leaderboard Management.

Uses Redis sorted sets for real-time, low-latency leaderboard
operations.  Falls back to an in-memory implementation when Redis
is unavailable (useful for tests / local dev).
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class Leaderboard:
    """Real-time leaderboard backed by Redis sorted sets.

    Parameters
    ----------
    redis_url:
        Redis connection string.  When *None* an in-memory fallback
        is used automatically.
    key_prefix:
        Prefix for Redis keys (default ``truenorth:leaderboard``).
    """

    def __init__(
        self,
        redis_url: str | None = None,
        key_prefix: str = "truenorth:leaderboard",
    ) -> None:
        self.redis_url = redis_url
        self.key_prefix = key_prefix
        self._redis: Any | None = None
        self._memory: dict[str, dict[str, int]] = {}  # fallback store
        self._history: dict[str, list[dict[str, Any]]] = {}

        if redis_url:
            try:
                import redis as redis_lib  # type: ignore[import-untyped]

                self._redis = redis_lib.from_url(
                    redis_url, decode_responses=True
                )
                self._redis.ping()
                logger.info("Leaderboard connected to Redis at %s", redis_url)
            except Exception:
                logger.warning(
                    "Redis unavailable at %s — using in-memory fallback",
                    redis_url,
                )
                self._redis = None

    # ── helpers ─────────────────────────────────────────────

    def _key(self, exercise_id: str) -> str:
        return f"{self.key_prefix}:{exercise_id}"

    def _history_key(self, team_id: str) -> str:
        return f"{self.key_prefix}:history:{team_id}"

    # ── update score ────────────────────────────────────────

    async def update_score(
        self,
        exercise_id: str,
        team_id: str,
        score: int,
    ) -> None:
        """Set the score for *team_id* in the given exercise."""
        if self._redis is not None:
            self._redis.zadd(self._key(exercise_id), {team_id: score})
            # Append to history
            self._redis.rpush(
                self._history_key(team_id),
                f"{exercise_id}:{score}:{int(time.time())}",
            )
        else:
            self._memory.setdefault(exercise_id, {})[team_id] = score
            self._history.setdefault(team_id, []).append(
                {
                    "exercise_id": exercise_id,
                    "score": score,
                    "timestamp": int(time.time()),
                }
            )

    # ── get leaderboard ────────────────────────────────────

    async def get_leaderboard(
        self,
        exercise_id: str,
        top_n: int = 10,
    ) -> list[dict[str, Any]]:
        """Return the top *N* entries for an exercise.

        Each entry: ``{rank, team_id, score}``.
        """
        if self._redis is not None:
            raw = self._redis.zrevrange(
                self._key(exercise_id), 0, top_n - 1, withscores=True
            )
            return [
                {"rank": i + 1, "team_id": tid, "score": int(sc)}
                for i, (tid, sc) in enumerate(raw)
            ]

        board = self._memory.get(exercise_id, {})
        sorted_board = sorted(board.items(), key=lambda kv: kv[1], reverse=True)
        return [
            {"rank": i + 1, "team_id": tid, "score": sc}
            for i, (tid, sc) in enumerate(sorted_board[:top_n])
        ]

    # ── get team rank ──────────────────────────────────────

    async def get_team_rank(
        self,
        exercise_id: str,
        team_id: str,
    ) -> int:
        """Return 1-based rank of *team_id* (0 if not found)."""
        if self._redis is not None:
            rank = self._redis.zrevrank(self._key(exercise_id), team_id)
            return (rank + 1) if rank is not None else 0

        board = self._memory.get(exercise_id, {})
        if team_id not in board:
            return 0
        sorted_teams = sorted(board.items(), key=lambda kv: kv[1], reverse=True)
        for i, (tid, _) in enumerate(sorted_teams):
            if tid == team_id:
                return i + 1
        return 0

    # ── history ─────────────────────────────────────────────

    async def get_historical(
        self,
        team_id: str,
    ) -> list[dict[str, Any]]:
        """Return chronological history for *team_id*."""
        if self._redis is not None:
            raw = self._redis.lrange(self._history_key(team_id), 0, -1)
            results: list[dict[str, Any]] = []
            for entry in raw:
                parts = entry.split(":")
                if len(parts) == 3:
                    results.append(
                        {
                            "exercise_id": parts[0],
                            "score": int(parts[1]),
                            "timestamp": int(parts[2]),
                        }
                    )
            return results

        return list(self._history.get(team_id, []))

    # ── cleanup ─────────────────────────────────────────────

    async def clear(self, exercise_id: str) -> None:
        """Remove the leaderboard for an exercise."""
        if self._redis is not None:
            self._redis.delete(self._key(exercise_id))
        else:
            self._memory.pop(exercise_id, None)