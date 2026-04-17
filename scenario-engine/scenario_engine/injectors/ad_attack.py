"""TrueNorth Range - Active Directory attack injector.

Simulates common AD attack techniques for training scenarios:
  - Kerberoasting
  - Password spraying
  - DCSync
  - Group Policy modification
"""

from __future__ import annotations

import hashlib
import logging
import random
import string
from typing import Any

from .base import BaseInjector

logger = logging.getLogger(__name__)


class ADAttackInjector(BaseInjector):
    """Inject Active Directory attack simulations into a range exercise."""

    name: str = "ad_attack"
    description: str = (
        "Simulates Active Directory attacks including Kerberoasting, password spray, DCSync, and GPO modification."
    )
    required_params: list[str] = ["attack_type", "target_dc"]

    # Recognised attack types and their MITRE ATT&CK technique IDs
    _TECHNIQUE_MAP: dict[str, str] = {
        "kerberoast": "T1558.003",
        "password_spray": "T1110.003",
        "dcsync": "T1003.006",
        "gpo_modify": "T1484.001",
    }

    def validate_params(self) -> None:
        for p in self.required_params:
            assert p in self.params, f"Missing required param: {p}"
        attack = self.params["attack_type"]
        assert attack in self._TECHNIQUE_MAP, (
            f"Unknown attack_type '{attack}'. Valid: {list(self._TECHNIQUE_MAP.keys())}"
        )

    def execute(self, context: dict[str, Any]) -> dict[str, Any]:
        self.validate_params()
        attack = self.params["attack_type"]
        handler = {
            "kerberoast": self._kerberoast,
            "password_spray": self._password_spray,
            "dcsync": self._dcsync,
            "gpo_modify": self._gpo_modify,
        }[attack]
        result = handler(context)
        result.update(
            {
                "injector": self.name,
                "attack_type": attack,
                "technique_id": self._TECHNIQUE_MAP[attack],
                "target_dc": self.params["target_dc"],
            }
        )
        logger.info(
            "AD attack '%s' (%s) executed against %s",
            attack,
            self._TECHNIQUE_MAP[attack],
            self.params["target_dc"],
        )
        return result

    # ------------------------------------------------------------------
    # Attack simulations
    # ------------------------------------------------------------------

    def _kerberoast(self, context: dict[str, Any]) -> dict[str, Any]:
        """Simulate Kerberoasting: request TGS tickets for SPN accounts."""
        target_spns = self.params.get(
            "target_spns",
            [
                "MSSQLSvc/sql01.corp.truenorth.local:1433",
                "HTTP/web01.corp.truenorth.local",
                "exchangeMDB/exch01.corp.truenorth.local",
            ],
        )
        results = []
        for spn in target_spns:
            ticket_hash = hashlib.sha256(f"{spn}:{random.random()}".encode()).hexdigest()[:32]
            results.append(
                {
                    "spn": spn,
                    "ticket_hash": f"$krb5tgs$23$*{ticket_hash}",
                    "crackable": random.choice([True, False]),
                }
            )
        return {
            "spns_targeted": len(target_spns),
            "tickets_obtained": results,
            "tool": "Rubeus / GetUserSPNs.py",
        }

    def _password_spray(self, context: dict[str, Any]) -> dict[str, Any]:
        """Simulate password spraying against AD accounts."""
        users = self.params.get(
            "target_users",
            [
                "admin",
                "jdoe",
                "svc_backup",
                "svc_sql",
                "trainee",
            ],
        )
        passwords = self.params.get(
            "passwords",
            [
                "Welcome1!",
                "Password1!",
                "Spring2026!",
            ],
        )
        success_rate = float(self.params.get("success_rate", 0.15))
        attempts: list[dict[str, Any]] = []
        for pwd in passwords:
            for user in users:
                success = random.random() < success_rate
                attempts.append(
                    {
                        "username": user,
                        "password_tested": pwd[:3] + "***",
                        "success": success,
                    }
                )
        successful = [a for a in attempts if a["success"]]
        return {
            "total_attempts": len(attempts),
            "successful": len(successful),
            "compromised_accounts": [a["username"] for a in successful],
            "lockouts_triggered": max(0, len(users) - 3),
            "tool": "Spray / CrackMapExec",
        }

    def _dcsync(self, context: dict[str, Any]) -> dict[str, Any]:
        """Simulate DCSync replication attack."""
        target_accounts = self.params.get(
            "target_accounts",
            [
                "krbtgt",
                "Administrator",
                "svc_backup",
            ],
        )
        results = []
        for account in target_accounts:
            nt_hash = hashlib.md5(f"{account}:{random.random()}".encode()).hexdigest()
            results.append(
                {
                    "account": account,
                    "nt_hash": nt_hash,
                    "lm_hash": "aad3b435b51404eeaad3b435b51404ee",
                }
            )
        return {
            "accounts_synced": len(results),
            "hashes": results,
            "replication_rights_used": "DS-Replication-Get-Changes-All",
            "tool": "Mimikatz / secretsdump.py",
        }

    def _gpo_modify(self, context: dict[str, Any]) -> dict[str, Any]:
        """Simulate Group Policy Object modification for persistence."""
        gpo_name = self.params.get("gpo_name", "Default Domain Policy")
        modification = self.params.get("modification", "scheduled_task")
        gpo_guid = "".join(random.choices(string.hexdigits[:16], k=32))
        return {
            "gpo_name": gpo_name,
            "gpo_guid": f"{{{gpo_guid[:8]}-{gpo_guid[8:12]}-{gpo_guid[12:16]}-{gpo_guid[16:20]}-{gpo_guid[20:]}}}",
            "modification_type": modification,
            "payload": {
                "scheduled_task": {
                    "name": "WindowsUpdate",
                    "command": "powershell.exe -enc <base64_payload>",
                    "trigger": "AtLogon",
                },
                "startup_script": {
                    "script_path": "\\\\dc01\\SYSVOL\\corp.truenorth.local\\scripts\\update.ps1",
                },
            }.get(modification, {"type": modification}),
            "tool": "SharpGPOAbuse / PowerView",
        }
