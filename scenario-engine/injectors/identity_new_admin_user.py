"""Identity inject — creates a new admin user on target (simulated)."""

from __future__ import annotations

import logging

from . import Injector, InjectResult, RangeContext, register_injector

logger = logging.getLogger(__name__)


@register_injector
class IdentityNewAdminInjector(Injector):
    action_name = "identity_new_admin_user"

    def execute(self, params: dict, ctx: RangeContext) -> InjectResult:
        username = params.get("username", "backdoor_admin")
        target = params.get("target", "dc-01")
        target_ip = None
        for vm in ctx.vms:
            if vm.get("name") == target or vm.get("role") == "dc":
                target_ip = vm["ip"]
                break
        target_ip = target_ip or "10.0.2.10"

        logger.info(f"Creating admin user '{username}' on {target} ({target_ip})")
        # Real mode: SSH/WinRM to DC and create user
        # Mock mode: generate Windows Security event 4720 (user created)
        return InjectResult(
            success=True, action="identity_new_admin_user", detail=f"Created admin user '{username}' on {target}"
        )
