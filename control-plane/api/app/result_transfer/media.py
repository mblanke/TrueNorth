"""Bounded, non-overwriting removable-media transport for result packages."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .contract import DecryptedResult, ResultTransferError, decrypt_package, verify_package

PACKAGE_SUFFIX = ".tnresult.json"
MAX_PACKAGE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class MediaPackage:
    path: Path
    package: dict
    sha256: str


@dataclass(frozen=True)
class Consumption:
    status: Literal["applied", "duplicate"]
    package_id: uuid.UUID
    sha256: str


def encode_package(package: dict) -> bytes:
    """Canonical on-media bytes used for package digests and collision detection."""
    return (json.dumps(package, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def _package_id(package: dict) -> uuid.UUID:
    try:
        return uuid.UUID(str(package["package_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ResultTransferError("package_id is missing or invalid") from exc


def export_package(
    package: dict,
    directory: Path,
    *,
    trusted_signing_public_key: str,
    destination_enclave: str,
) -> MediaPackage:
    """Write one verified package with an exclusive create and restrictive permissions."""
    if not verify_package(
        package,
        trusted_signing_public_key=trusted_signing_public_key,
        destination_enclave=destination_enclave,
    ):
        raise ResultTransferError("refusing to export an invalid or misdirected package")
    package_id = _package_id(package)
    content = encode_package(package)
    if len(content) > MAX_PACKAGE_BYTES:
        raise ResultTransferError("package exceeds the removable-media size limit")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not stat.S_ISDIR(directory.lstat().st_mode):
        raise ResultTransferError("result export destination must be a real directory")
    path = directory / f"{package_id}{PACKAGE_SUFFIX}"
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except FileExistsError as exc:
        raise ResultTransferError(f"refusing to overwrite existing package {package_id}") from exc
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return MediaPackage(path=path, package=package, sha256=hashlib.sha256(content).hexdigest())


def load_package(
    path: Path,
    *,
    trusted_signing_public_key: str,
    destination_enclave: str,
) -> MediaPackage:
    """Read a regular bounded file, enforce filename binding, then verify its signature."""
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise ResultTransferError("result package must be a readable regular file, not a link or device") from exc
    try:
        with os.fdopen(descriptor, "rb") as stream:
            file_stat = os.fstat(stream.fileno())
            if not stat.S_ISREG(file_stat.st_mode):
                raise ResultTransferError("result package must be a regular file, not a link or device")
            if file_stat.st_size > MAX_PACKAGE_BYTES:
                raise ResultTransferError("package exceeds the removable-media size limit")
            content = stream.read(MAX_PACKAGE_BYTES + 1)
        if len(content) > MAX_PACKAGE_BYTES:
            raise ResultTransferError("package exceeds the removable-media size limit")
        value = json.loads(content)
    except ResultTransferError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResultTransferError("result package is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ResultTransferError("result package must be a JSON object")
    package_id = _package_id(value)
    if path.name != f"{package_id}{PACKAGE_SUFFIX}":
        raise ResultTransferError("result package filename does not match its signed package_id")
    if not verify_package(
        value,
        trusted_signing_public_key=trusted_signing_public_key,
        destination_enclave=destination_enclave,
    ):
        raise ResultTransferError("result package signature or destination is invalid")
    return MediaPackage(path=path, package=value, sha256=hashlib.sha256(content).hexdigest())


class FileConsumptionLedger:
    """A receipt ledger containing package ids and hashes, never names or grades."""

    def __init__(self, directory: Path):
        self.directory = directory

    def check(self, package_id: uuid.UUID, sha256: str) -> bool:
        """Return True for an already-consumed identical package; reject id collisions."""
        receipt = self.directory / f"{package_id}.sha256"
        try:
            descriptor = os.open(receipt, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise ResultTransferError("consumption ledger receipt must be a regular file") from exc
        try:
            with os.fdopen(descriptor, "rb") as stream:
                file_stat = os.fstat(stream.fileno())
                if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > 65:
                    raise ResultTransferError("consumption ledger receipt is invalid")
                recorded = stream.read(66).decode("ascii").strip()
        except ResultTransferError:
            raise
        except (OSError, UnicodeDecodeError) as exc:
            raise ResultTransferError("consumption ledger receipt is invalid") from exc
        if recorded != sha256:
            raise ResultTransferError("package_id was already consumed with different content")
        return True

    def record(self, package_id: uuid.UUID, sha256: str) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not stat.S_ISDIR(self.directory.lstat().st_mode):
            raise ResultTransferError("consumption ledger must be a real directory")
        receipt = self.directory / f"{package_id}.sha256"
        try:
            descriptor = os.open(
                receipt,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError as exc:
            if not self.check(package_id, sha256):
                raise ResultTransferError("consumption receipt could not be recorded") from exc
            return
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(f"{sha256}\n")
            stream.flush()
            os.fsync(stream.fileno())


def consume_package(
    path: Path,
    *,
    trusted_signing_public_key: str,
    recipient_private_key: str,
    destination_enclave: str,
    ledger: FileConsumptionLedger,
    apply: Callable[[DecryptedResult], None],
) -> Consumption:
    """Verify and decrypt once, then invoke an idempotent Secret-side result writer.

    ``apply`` must persist by ``DecryptedResult.package_id`` idempotently. This closes the
    crash window between its database commit and the file receipt being recorded.
    """
    loaded = load_package(
        path,
        trusted_signing_public_key=trusted_signing_public_key,
        destination_enclave=destination_enclave,
    )
    package_id = _package_id(loaded.package)
    if ledger.check(package_id, loaded.sha256):
        return Consumption(status="duplicate", package_id=package_id, sha256=loaded.sha256)
    result = decrypt_package(
        loaded.package,
        trusted_signing_public_key=trusted_signing_public_key,
        recipient_private_key=recipient_private_key,
        destination_enclave=destination_enclave,
    )
    apply(result)
    ledger.record(package_id, loaded.sha256)
    return Consumption(status="applied", package_id=package_id, sha256=loaded.sha256)
