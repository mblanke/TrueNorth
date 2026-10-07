"""Greyspace request and response shapes (published in docs/interfaces/openapi.json)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CorpusTier = Literal["t0", "t1", "t2", "full"]
NpcProfile = Literal["off", "office-day", "quiet-night"]
GreyspaceStatus = Literal["not_attached", "configured", "deployed", "pending_infrastructure", "failed"]


class GreyspaceBlock(BaseModel):
    """A range's Greyspace block: the template ``greyspace:`` key, or attached through the API.
    Same fields as ``scenario-engine/schemas/template.schema.json#/properties/greyspace``."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    corpus_tier: CorpusTier = "t0"
    site_packs: list[str] | None = Field(
        None, description="Site categories to serve (news, search, social, ...). Omit for all.", max_length=64
    )
    public_prefix: str | None = Field(
        None, description="IPv4 CIDR covering every ISP prefix of the corpus. Omit to use the corpus's own."
    )
    npc_profile: NpcProfile = Field("off", description="Simulated users. Recorded only: NPC traffic is a later slice.")
    threat_infra: bool = Field(True, description="Serve the corpus's threat-actor domains (C2, phishing stubs).")
    trust_ca: bool = Field(True, description="Recorded only: the Greyspace root CA is a later slice.")


class CorpusSummary(BaseModel):
    tier: CorpusTier
    title: str
    cap_bytes: int | None
    location: str
    builder: str
    description: str
    available: bool = Field(description="Whether the control plane can read this tier's manifest.")
    version: str | None = None
    sites: int | None = None
    bytes: int | None = None
    categories: dict[str, int] | None = None
    threat_domains: int | None = None


class GreyspaceStatusOut(BaseModel):
    range_id: uuid.UUID
    attached: bool
    status: GreyspaceStatus
    range_state: str
    block: GreyspaceBlock | None = None
    template_block: GreyspaceBlock | None = Field(
        None, description="The block the range's template declares, if any; attaching with no body uses it."
    )
    corpus: CorpusSummary | None = None
    detail: dict | None = None
    problems: list[str] = Field(default_factory=list, description="Why the block cannot render on its corpus now.")
    deployed_at: datetime | None = None
    updated_at: datetime | None = None


class GreyspaceConfigOut(BaseModel):
    range_id: uuid.UUID
    corpus_tier: CorpusTier
    corpus_version: str
    address_plan: dict
    isps: list[dict]
    services: list[str]
    sites: int
    site_packs: list[str]
    tlds: list[str]
    zones: list[str]
    threat_domains: list[dict]
    files: list[str]
