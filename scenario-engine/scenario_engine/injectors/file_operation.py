"""TrueNorth Range - File system operation injector.

Simulates malicious file operations for training scenarios:
  - Create suspicious files (mimikatz, psexec, etc.)
  - Modify system files
  - Delete security logs
  - Encrypt files (ransomware simulation)
"""

from __future__ import annotations

import hashlib
import logging
import random
import string
from typing import Any

from .base import BaseInjector

logger = logging.getLogger(__name__)


class FileOperationInjector(BaseInjector):
    """Inject file system operation simulations into a range exercise."""

    name: str = "file_operation"
    description: str = (
        "Simulates malicious file operations: dropping attacker tools, "
        "modifying system files, deleting logs, and ransomware encryption."
    )
    required_params: list[str] = ["operation", "target_host"]

    _OPERATIONS = (
        "drop_tools",
        "modify_system",
        "delete_logs",
        "ransomware_encrypt",
    )

    # Common attacker tools for the drop_tools operation
    _ATTACKER_TOOLS: list[dict[str, str]] = [
        {"name": "mimikatz.exe", "hash_prefix": "abbe2", "technique": "T1003.001"},
        {"name": "psexec.exe", "hash_prefix": "3b4f0", "technique": "T1569.002"},
        {"name": "rubeus.exe", "hash_prefix": "d7e1a", "technique": "T1558.003"},
        {"name": "sharphound.exe", "hash_prefix": "9c21f", "technique": "T1087.002"},
        {"name": "procdump.exe", "hash_prefix": "1a2b3", "technique": "T1003.001"},
        {"name": "lazagne.exe", "hash_prefix": "f8e7d", "technique": "T1555"},
        {"name": "nc.exe", "hash_prefix": "55c2e", "technique": "T1095"},
        {"name": "chisel.exe", "hash_prefix": "7bc4d", "technique": "T1572"},
    ]

    def validate_params(self) -> None:
        for p in self.required_params:
            assert p in self.params, f"Missing required param: {p}"
        op = self.params["operation"]
        assert op in self._OPERATIONS, f"Unknown operation '{op}'. Valid: {list(self._OPERATIONS)}"

    def execute(self, context: dict[str, Any]) -> dict[str, Any]:
        self.validate_params()
        op = self.params["operation"]
        handler = {
            "drop_tools": self._drop_tools,
            "modify_system": self._modify_system,
            "delete_logs": self._delete_logs,
            "ransomware_encrypt": self._ransomware_encrypt,
        }[op]
        result = handler(context)
        result.update(
            {
                "injector": self.name,
                "operation": op,
                "target_host": self.params["target_host"],
            }
        )
        logger.info(
            "File operation '%s' executed on %s",
            op,
            self.params["target_host"],
        )
        return result

    # ------------------------------------------------------------------
    # Operation simulations
    # ------------------------------------------------------------------

    def _drop_tools(self, context: dict[str, Any]) -> dict[str, Any]:
        """Simulate dropping attacker tools onto a target host."""
        tools_to_drop = self.params.get("tools", None)
        if tools_to_drop:
            selected = [t for t in self._ATTACKER_TOOLS if t["name"] in tools_to_drop]
        else:
            count = int(self.params.get("tool_count", 3))
            selected = random.sample(self._ATTACKER_TOOLS, min(count, len(self._ATTACKER_TOOLS)))

        drop_path = self.params.get("drop_path", "C:\\Windows\\Temp")
        dropped: list[dict[str, Any]] = []
        for tool in selected:
            file_hash = hashlib.sha256(f"{tool['name']}:{random.random()}".encode()).hexdigest()
            dropped.append(
                {
                    "filename": tool["name"],
                    "path": f"{drop_path}\\{tool['name']}",
                    "sha256": file_hash,
                    "size_bytes": random.randint(50_000, 2_000_000),
                    "technique_id": tool["technique"],
                    "detected": random.choice([True, False]),
                }
            )

        return {
            "tools_dropped": len(dropped),
            "files": dropped,
            "drop_path": drop_path,
            "technique_id": "T1105",
        }

    def _modify_system(self, context: dict[str, Any]) -> dict[str, Any]:
        """Simulate modifying critical system files."""
        target_files = self.params.get(
            "target_files",
            [
                {"path": "C:\\Windows\\System32\\drivers\\etc\\hosts", "modification": "dns_redirect"},
                {"path": "/etc/passwd", "modification": "add_backdoor_user"},
                {"path": "C:\\Windows\\System32\\config\\SAM", "modification": "credential_access"},
                {"path": "/etc/sudoers", "modification": "privilege_escalation"},
            ],
        )

        modifications: list[dict[str, Any]] = []
        for tf in target_files:
            modifications.append(
                {
                    "file_path": tf["path"],
                    "modification_type": tf["modification"],
                    "original_hash": hashlib.sha256(tf["path"].encode()).hexdigest()[:16],
                    "modified_hash": hashlib.sha256(f"{tf['path']}:modified".encode()).hexdigest()[:16],
                    "backup_created": random.choice([True, False]),
                    "timestamp": "2026-02-26T10:30:00Z",
                }
            )

        return {
            "files_modified": len(modifications),
            "modifications": modifications,
            "technique_id": "T1565.001",
        }

    def _delete_logs(self, context: dict[str, Any]) -> dict[str, Any]:
        """Simulate deletion of security and audit logs."""
        log_sources = self.params.get(
            "log_sources",
            [
                {
                    "name": "Security",
                    "type": "windows_event_log",
                    "path": "C:\\Windows\\System32\\winevt\\Logs\\Security.evtx",
                },
                {
                    "name": "System",
                    "type": "windows_event_log",
                    "path": "C:\\Windows\\System32\\winevt\\Logs\\System.evtx",
                },
                {
                    "name": "PowerShell",
                    "type": "windows_event_log",
                    "path": "C:\\Windows\\System32\\winevt\\Logs\\PowerShell.evtx",
                },
                {"name": "syslog", "type": "linux_log", "path": "/var/log/syslog"},
                {"name": "auth.log", "type": "linux_log", "path": "/var/log/auth.log"},
            ],
        )

        cleared: list[dict[str, Any]] = []
        for source in log_sources:
            events_cleared = random.randint(500, 50_000)
            cleared.append(
                {
                    "log_name": source["name"],
                    "log_type": source["type"],
                    "log_path": source["path"],
                    "events_cleared": events_cleared,
                    "method": random.choice(["wevtutil cl", "Clear-EventLog", "rm -f", "truncate"]),
                    "success": random.random() > 0.1,
                }
            )

        return {
            "logs_targeted": len(cleared),
            "logs_cleared": cleared,
            "technique_id": "T1070.001",
            "anti_forensics": True,
        }

    def _ransomware_encrypt(self, context: dict[str, Any]) -> dict[str, Any]:
        """Simulate ransomware file encryption (NO actual encryption)."""
        target_dirs = self.params.get(
            "target_directories",
            [
                "C:\\Users\\trainee\\Documents",
                "C:\\Users\\trainee\\Desktop",
                "D:\\SharedFiles",
            ],
        )
        extensions = self.params.get(
            "target_extensions",
            [
                ".doc",
                ".docx",
                ".xls",
                ".xlsx",
                ".pdf",
                ".ppt",
                ".jpg",
                ".png",
                ".txt",
                ".csv",
            ],
        )
        file_count = int(self.params.get("file_count", 150))
        ransom_ext = self.params.get("ransom_extension", ".encrypted")

        encrypted_files: list[dict[str, str]] = []
        for _i in range(file_count):
            ext = random.choice(extensions)
            directory = random.choice(target_dirs)
            filename = "".join(random.choices(string.ascii_lowercase, k=8)) + ext
            encrypted_files.append(
                {
                    "original_path": f"{directory}\\{filename}",
                    "encrypted_path": f"{directory}\\{filename}{ransom_ext}",
                    "original_extension": ext,
                }
            )

        ransom_note = (
            "=== YOUR FILES HAVE BEEN ENCRYPTED ===\n"
            "This is a TrueNorth Range training simulation.\n"
            "Contact range admin for decryption keys.\n"
            f"Victim ID: {hashlib.sha256(self.params['target_host'].encode()).hexdigest()[:16]}"
        )

        return {
            "files_encrypted": len(encrypted_files),
            "sample_files": encrypted_files[:10],
            "target_directories": target_dirs,
            "ransom_extension": ransom_ext,
            "ransom_note": ransom_note,
            "technique_id": "T1486",
            "simulation_only": True,
        }
