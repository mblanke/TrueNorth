"""Shared helpers: paths, YAML/JSON loading, hashing, TBD detection, name rules."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Iterator

import yaml


class RangeError(Exception):
    """A definition, configuration or safety problem. Message is shown to the operator."""

    def __init__(self, errors: str | list[str]):
        if isinstance(errors, str):
            errors = [errors]
        self.errors = errors
        super().__init__("\n".join(f"  - {e}" for e in errors))


# Parent range IDs: CANARY, EXCOURSE, R-0001. Instance IDs append -Snn for students.
RANGE_ID_RE = re.compile(r"^[A-Z][A-Z0-9]{1,11}(-[0-9]{1,4})?$")
INSTANCE_ID_RE = re.compile(r"^[A-Z][A-Z0-9]{1,11}(-[0-9]{1,4})?(-S[0-9]{2})?$")
LOCAL_NAME_RE = re.compile(r"^[a-z][a-z0-9]{0,15}$")
ALIAS_RE = re.compile(r"^[a-z][a-z0-9]{0,9}$")
QUALIFIED_NAME_RE = re.compile(r"^[a-z][a-z0-9]{0,15}(-[a-z][a-z0-9]{0,15})?$")


def repo_root() -> Path:
    env = os.environ.get("TNRANGE_ROOT")
    if env:
        return Path(env).resolve()
    # tools/tnrange/common.py -> repo root is two levels above tools/
    return Path(__file__).resolve().parents[2]


def load_yaml(path: Path) -> Any:
    if not path.exists():
        raise RangeError(f"file not found: {path}")
    with path.open() as fh:
        try:
            data = yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            raise RangeError(f"{path}: invalid YAML: {exc}") from exc
    return data if data is not None else {}


def load_json(path: Path) -> Any:
    if not path.exists():
        raise RangeError(f"file not found: {path}")
    with path.open() as fh:
        try:
            return json.load(fh)
        except json.JSONDecodeError as exc:
            raise RangeError(f"{path}: invalid JSON: {exc}") from exc


def _default(obj: Any) -> Any:
    if isinstance(obj, (_dt.date, _dt.datetime)):
        return obj.isoformat()
    raise TypeError(f"not JSON serialisable: {type(obj)!r}")


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_default)


def sha256_obj(obj: Any) -> str:
    return hashlib.sha256(canonical(obj).encode()).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True, default=_default) + "\n")
    tmp.replace(path)


def is_tbd(value: Any) -> bool:
    return isinstance(value, str) and value.strip().upper() == "TBD"


def find_tbd(obj: Any, prefix: str = "") -> Iterator[str]:
    if is_tbd(obj):
        yield prefix or "<root>"
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from find_tbd(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from find_tbd(v, f"{prefix}[{i}]")


def to_date(value: Any) -> _dt.date | None:
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    if isinstance(value, str):
        try:
            return _dt.date.fromisoformat(value)
        except ValueError:
            return None
    return None


def now_utc() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def instance_ids(range_id: str, students: int) -> list[str]:
    if students <= 1:
        return [range_id]
    return [f"{range_id}-S{n:02d}" for n in range(1, students + 1)]
