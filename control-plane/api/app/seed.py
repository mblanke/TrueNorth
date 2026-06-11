"""TrueNorth Range -- Seed data for nations, coalitions, auth zones, AI.

42 nations (32 NATO + 10 key partners), 5 coalitions, 3 default auth zones,
1 Ollama AI backend. No hardware is seeded -- hypervisors, storage, and
network devices are registered once they actually exist and are reachable.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session

from .models import (
    AIBackendConfig,
    AuthZonePolicy,
    Coalition,
    CoalitionMembership,
    HypervisorConnection,
    Nation,
    NetworkDevice,
    StorageAppliance,
    StorageVolume,
    Tenant,
)

logger = logging.getLogger(__name__)

# fmt: off
NATO_NATIONS = [
    ("Albania",        "AL", "ALB", "\U0001f1e6\U0001f1f1", True,  False),
    ("Belgium",        "BE", "BEL", "\U0001f1e7\U0001f1ea", True,  False),
    ("Bulgaria",       "BG", "BGR", "\U0001f1e7\U0001f1ec", True,  False),
    ("Canada",         "CA", "CAN", "\U0001f1e8\U0001f1e6", True,  True),
    ("Croatia",        "HR", "HRV", "\U0001f1ed\U0001f1f7", True,  False),
    ("Czech Republic", "CZ", "CZE", "\U0001f1e8\U0001f1ff", True,  False),
    ("Denmark",        "DK", "DNK", "\U0001f1e9\U0001f1f0", True,  False),
    ("Estonia",        "EE", "EST", "\U0001f1ea\U0001f1ea", True,  False),
    ("Finland",        "FI", "FIN", "\U0001f1eb\U0001f1ee", True,  False),
    ("France",         "FR", "FRA", "\U0001f1eb\U0001f1f7", True,  False),
    ("Germany",        "DE", "DEU", "\U0001f1e9\U0001f1ea", True,  False),
    ("Greece",         "GR", "GRC", "\U0001f1ec\U0001f1f7", True,  False),
    ("Hungary",        "HU", "HUN", "\U0001f1ed\U0001f1fa", True,  False),
    ("Iceland",        "IS", "ISL", "\U0001f1ee\U0001f1f8", True,  False),
    ("Italy",          "IT", "ITA", "\U0001f1ee\U0001f1f9", True,  False),
    ("Latvia",         "LV", "LVA", "\U0001f1f1\U0001f1fb", True,  False),
    ("Lithuania",      "LT", "LTU", "\U0001f1f1\U0001f1f9", True,  False),
    ("Luxembourg",     "LU", "LUX", "\U0001f1f1\U0001f1fa", True,  False),
    ("Montenegro",     "ME", "MNE", "\U0001f1f2\U0001f1ea", True,  False),
    ("Netherlands",    "NL", "NLD", "\U0001f1f3\U0001f1f1", True,  False),
    ("North Macedonia","MK", "MKD", "\U0001f1f2\U0001f1f0", True,  False),
    ("Norway",         "NO", "NOR", "\U0001f1f3\U0001f1f4", True,  False),
    ("Poland",         "PL", "POL", "\U0001f1f5\U0001f1f1", True,  False),
    ("Portugal",       "PT", "PRT", "\U0001f1f5\U0001f1f9", True,  False),
    ("Romania",        "RO", "ROU", "\U0001f1f7\U0001f1f4", True,  False),
    ("Slovakia",       "SK", "SVK", "\U0001f1f8\U0001f1f0", True,  False),
    ("Slovenia",       "SI", "SVN", "\U0001f1f8\U0001f1ee", True,  False),
    ("Spain",          "ES", "ESP", "\U0001f1ea\U0001f1f8", True,  False),
    ("Sweden",         "SE", "SWE", "\U0001f1f8\U0001f1ea", True,  False),
    ("Turkey",         "TR", "TUR", "\U0001f1f9\U0001f1f7", True,  False),
    ("United Kingdom", "GB", "GBR", "\U0001f1ec\U0001f1e7", True,  True),
    ("United States",  "US", "USA", "\U0001f1fa\U0001f1f8", True,  True),
]

PARTNER_NATIONS = [
    ("Australia",      "AU", "AUS", "\U0001f1e6\U0001f1fa", False, True),
    ("New Zealand",    "NZ", "NZL", "\U0001f1f3\U0001f1ff", False, True),
    ("Japan",          "JP", "JPN", "\U0001f1ef\U0001f1f5", False, False),
    ("South Korea",    "KR", "KOR", "\U0001f1f0\U0001f1f7", False, False),
    ("Israel",         "IL", "ISR", "\U0001f1ee\U0001f1f1", False, False),
    ("Singapore",      "SG", "SGP", "\U0001f1f8\U0001f1ec", False, False),
    ("Ukraine",        "UA", "UKR", "\U0001f1fa\U0001f1e6", False, False),
    ("Georgia",        "GE", "GEO", "\U0001f1ec\U0001f1ea", False, False),
    ("Colombia",       "CO", "COL", "\U0001f1e8\U0001f1f4", False, False),
    ("India",          "IN", "IND", "\U0001f1ee\U0001f1f3", False, False),
]
# fmt: on

COALITIONS = [
    ("Five Eyes", "fvey", "SIGINT alliance: US, UK, CA, AU, NZ"),
    ("NATO", "nato", "North Atlantic Treaty Organization"),
    ("ABCANZ", "abcanz", "America, Britain, Canada, Australia, New Zealand Armies Programme"),
    ("CANZUK", "canzuk", "Canada, Australia, New Zealand, United Kingdom"),
    ("Combined Ops", "custom", "Custom combined exercise coalition"),
]

FVEY_CODES = {"US", "GB", "CA", "AU", "NZ"}
ABCANZ_CODES = {"US", "GB", "CA", "AU", "NZ"}
CANZUK_CODES = {"CA", "AU", "NZ", "GB"}


def seed_nations_and_coalitions(db: Session) -> None:
    """Insert nations and coalitions if the nations table is empty."""
    if db.query(Nation).count() > 0:
        return

    all_nations = NATO_NATIONS + PARTNER_NATIONS
    nation_map: dict[str, Nation] = {}

    for name, a2, a3, flag, is_nato, is_fvey in all_nations:
        n = Nation(
            name=name,
            iso_alpha2=a2,
            iso_alpha3=a3,
            flag_emoji=flag,
            is_nato=is_nato,
            is_fvey=is_fvey,
        )
        db.add(n)
        nation_map[a2] = n

    db.flush()
    logger.info("Seeded %d nations (32 NATO + 10 partners)", len(all_nations))

    # Coalitions
    coalition_map: dict[str, Coalition] = {}
    for cname, slug, desc in COALITIONS:
        c = Coalition(name=cname, slug=slug, description=desc)
        db.add(c)
        coalition_map[slug] = c

    db.flush()

    # Memberships
    for a2, nation in nation_map.items():
        if nation.is_nato:
            db.add(CoalitionMembership(nation_id=nation.id, coalition_id=coalition_map["nato"].id))
        if a2 in FVEY_CODES:
            db.add(CoalitionMembership(nation_id=nation.id, coalition_id=coalition_map["fvey"].id))
        if a2 in ABCANZ_CODES:
            db.add(CoalitionMembership(nation_id=nation.id, coalition_id=coalition_map["abcanz"].id))
        if a2 in CANZUK_CODES:
            db.add(CoalitionMembership(nation_id=nation.id, coalition_id=coalition_map["canzuk"].id))

    db.commit()
    logger.info("Seeded %d coalitions with memberships", len(COALITIONS))


def seed_auth_zones(db: Session) -> None:
    """Insert default auth zone policies if empty."""
    if db.query(AuthZonePolicy).count() > 0:
        return

    zones = [
        AuthZonePolicy(
            zone_name="Unclassified Zone",
            description="Standard access for unclassified training environments",
            allowed_methods="password_mfa,fido2,session_token",
            require_mfa=False,
            session_timeout_minutes=480,
            clearance_required="unclassified",
        ),
        AuthZonePolicy(
            zone_name="Secret Zone",
            description="Elevated access requiring MFA for classified exercises",
            allowed_methods="fido2,kerberos",
            require_mfa=True,
            session_timeout_minutes=120,
            clearance_required="secret",
        ),
        AuthZonePolicy(
            zone_name="TS/SCI Zone",
            description="Maximum security zone for top-secret operations",
            allowed_methods="fido2",
            require_mfa=True,
            session_timeout_minutes=60,
            max_failed_attempts=3,
            clearance_required="ts_sci",
        ),
    ]
    for z in zones:
        db.add(z)
    db.commit()
    logger.info("Seeded %d default auth zone policies", len(zones))


def seed_infrastructure(db: Session) -> None:
    """Insert Proxmox connections plus storage + network if missing. Tenant-scoped."""
    seeded_any = False

    # Resolve tenant (create default dev tenant if none exist)
    tenant_row = db.query(Tenant.id).first()
    if tenant_row:
        tenant_id = tenant_row[0]
    else:
        tenant_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        tenant = Tenant(id=tenant_id, name="Dev Tenant", slug="dev", is_active=True)
        db.add(tenant)
        db.commit()
        logger.info("Created default dev tenant %s", tenant_id)

    # Backfill tenant_id on any existing infra rows
    updated = (
        db.query(HypervisorConnection)
        .filter(HypervisorConnection.tenant_id.is_(None))
        .update({HypervisorConnection.tenant_id: tenant_id}, synchronize_session=False)
    )
    if updated:
        db.commit()
        logger.info("Backfilled tenant_id on %d hypervisor connections", updated)

    updated = (
        db.query(StorageAppliance)
        .filter(StorageAppliance.tenant_id.is_(None))
        .update({StorageAppliance.tenant_id: tenant_id}, synchronize_session=False)
    )
    if updated:
        db.commit()
        logger.info("Backfilled tenant_id on %d storage appliances", updated)

    updated = (
        db.query(StorageVolume)
        .filter(StorageVolume.tenant_id.is_(None))
        .update({StorageVolume.tenant_id: tenant_id}, synchronize_session=False)
    )
    if updated:
        db.commit()
        logger.info("Backfilled tenant_id on %d storage volumes", updated)

    updated = (
        db.query(NetworkDevice)
        .filter(NetworkDevice.tenant_id.is_(None))
        .update({NetworkDevice.tenant_id: tenant_id}, synchronize_session=False)
    )
    if updated:
        db.commit()
        logger.info("Backfilled tenant_id on %d network devices", updated)

    # No hardware is seeded. Hypervisors, storage appliances, and network
    # devices are registered through the UI/API once they actually exist and
    # are reachable; the previous demo gear (two disconnected Supermicro
    # Proxmox boxes plus fictional storage/switches) was removed on purpose.
    if not seeded_any:
        logger.info("Infrastructure seed skipped (already present)")


def seed_ai_backends(db: Session) -> None:
    """Insert default AI backend configuration if empty."""
    if db.query(AIBackendConfig).count() > 0:
        return

    backends = [
        AIBackendConfig(
            name="Ollama (Primary)",
            backend_type="ollama",
            base_url="https://ai.guapo613.beer",
            is_active=True,
            is_primary=True,
            max_concurrent=4,
            timeout_seconds=120,
            notes="Self-hosted Ollama instance - default model: mistral",
        ),
    ]
    for b in backends:
        db.add(b)
    db.commit()
    logger.info("Seeded %d AI backend configs", len(backends))
