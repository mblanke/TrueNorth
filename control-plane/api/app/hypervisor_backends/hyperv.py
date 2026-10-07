"""Hyper-V connection backend via WinRM (legacy; vSphere is the range platform)."""

from __future__ import annotations

from datetime import datetime

from ..models import HypervisorConnection, HypervisorNode
from ..schemas import HypervisorTestResult
from ..secretbox import unseal
from .base import BaseHypervisorBackend


class HyperVBackend(BaseHypervisorBackend):
    kind = "hyperv"

    def check_connection(self, conn, db):
        return check_connection(conn, db)

    def discover(self, conn_id, conn, db):
        return discover(conn_id, conn, db)


def _session(conn: HypervisorConnection):
    import winrm  # type: ignore[import]

    scheme = "https" if conn.verify_ssl else "http"
    port = conn.port or 5985
    return winrm.Session(
        f"{scheme}://{conn.host}:{port}/wsman",
        auth=(conn.username, unseal(conn.password_encrypted) or ""),
        transport="ntlm",
        server_cert_validation="ignore",
    )


def check_connection(conn: HypervisorConnection, db) -> HypervisorTestResult:
    """Test a Hyper-V connection via WinRM."""
    try:
        session = _session(conn)
        result = session.run_ps("(Get-VMHost).Name; (Get-VM).Count")
        if result.status_code != 0:
            raise RuntimeError(result.std_err.decode(errors="replace")[:300])

        lines = result.std_out.decode(errors="replace").strip().splitlines()
        host_name = lines[0].strip() if lines else conn.host

        conn.is_active = True
        conn.last_seen_at = datetime.utcnow()
        db.commit()
        return HypervisorTestResult(
            success=True,
            message=f"Connected to Hyper-V host {host_name!r}",
            version="Hyper-V",
            nodes_found=1,
        )
    except ImportError:
        return HypervisorTestResult(
            success=False,
            message="pywinrm is not installed on the API server",
            version="n/a",
            nodes_found=0,
        )
    except Exception as exc:
        conn.is_active = False
        db.commit()
        return HypervisorTestResult(success=False, message=f"Connection failed: {exc}", version="n/a", nodes_found=0)


def discover(conn_id, conn: HypervisorConnection, db) -> dict:
    """Discover Hyper-V via WinRM — single-node implementation."""
    try:
        session = _session(conn)
        result = session.run_ps(
            "(Get-VMHost).Name; "
            "(Get-VM | Measure-Object).Count; "
            "(Get-VMHost).MemoryCapacity / 1GB; "
            "(Get-VMHost).LogicalProcessorCount"
        )
        if result.status_code != 0:
            raise RuntimeError(result.std_err.decode(errors="replace")[:300])

        lines = result.std_out.decode(errors="replace").strip().splitlines()
        node_name = lines[0].strip() if lines else conn.host
        vm_count = int(lines[1].strip()) if len(lines) > 1 and lines[1].strip().isdigit() else 0
        mem_gb = float(lines[2].strip()) if len(lines) > 2 else 0.0
        cpu_count = int(lines[3].strip()) if len(lines) > 3 and lines[3].strip().isdigit() else 0

        existing = db.query(HypervisorNode).filter_by(node_name=node_name).first()
        discovered = 0
        if existing:
            existing.status = "online"
            existing.memory_total_gb = round(mem_gb, 1)
            existing.cpu_total = cpu_count
            existing.vm_count = vm_count
        else:
            db.add(
                HypervisorNode(
                    connection_id=str(conn_id),
                    node_name=node_name,
                    ip_address=conn.host,
                    status="online",
                    cpu_total=cpu_count,
                    cpu_used=0.0,
                    memory_total_gb=round(mem_gb, 1),
                    memory_used_gb=0.0,
                    storage_total_gb=0.0,
                    storage_used_gb=0.0,
                    vm_count=vm_count,
                )
            )
            discovered += 1

        conn.is_active = True
        conn.last_seen_at = datetime.utcnow()
        db.commit()
        return {"message": f"Discovered Hyper-V host {node_name!r}", "nodes_discovered": discovered}
    except ImportError:
        return {"message": "pywinrm is not installed on the API server", "nodes_discovered": 0}
    except Exception as exc:
        return {"message": f"Hyper-V discovery failed: {exc}", "nodes_discovered": 0}
