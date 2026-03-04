import pathlib

MODELS = pathlib.Path(r"D:\Projects\Dev\TrueNorth Range\Core_Docs\control-plane\api\app\models.py")
content = MODELS.read_text(encoding="utf-8")
lines = content.splitlines()
print(f"Original lines: {len(lines)}")

# 1. Add Float to imports (before ForeignKey)
inserted = False
for i, line in enumerate(lines):
    if line.strip() == "ForeignKey,":
        lines.insert(i, "    Float,")
        inserted = True
        print(f"Inserted Float import at line {i}")
        break
if not inserted:
    print("WARNING: Could not find ForeignKey import line")

# 2. Find User tenant relationship and insert new columns before it
user_cols = [
    "    # -- Extended identity fields -------------------------------------------",
    '    first_name: Mapped[str | None] = mapped_column(String(100), nullable=True)',
    '    last_name: Mapped[str | None] = mapped_column(String(100), nullable=True)',
    '    rank: Mapped[str | None] = mapped_column(String(50), nullable=True)',
    '    service_branch: Mapped[str | None] = mapped_column(String(100), nullable=True)',
    '    nation_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("nations.id"), nullable=True)',
    '    clearance_level: Mapped[str] = mapped_column(String(50), default="unclassified")',
    '    unit: Mapped[str | None] = mapped_column(String(255), nullable=True)',
    '    callsign: Mapped[str | None] = mapped_column(String(50), nullable=True)',
    '    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True)',
    "    # -- AD / directory sync -----------------------------------------------",
    '    source: Mapped[str] = mapped_column(String(20), default="local")',
    '    ad_object_guid: Mapped[str | None] = mapped_column(String(36), unique=True, nullable=True)',
    '    ad_distinguished_name: Mapped[str | None] = mapped_column(String(512), nullable=True)',
    '    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)',
    "    # -- Auth preferences --------------------------------------------------",
    '    auth_method_preference: Mapped[str | None] = mapped_column(String(30), nullable=True)',
    '    timezone: Mapped[str] = mapped_column(String(50), default="UTC")',
]
inserted_user = False
for i, line in enumerate(lines):
    if "tenant: Mapped[Tenant] = relationship" in line and "users" in line:
        for j, new_line in enumerate(user_cols):
            lines.insert(i + j, new_line)
        inserted_user = True
        print(f"Inserted {len(user_cols)} User columns at line {i}")
        break
if not inserted_user:
    print("WARNING: Could not find User tenant relationship line")

# 3. Find Team class and insert new columns after tenant_id
team_cols = [
    '    description: Mapped[str | None] = mapped_column(Text, nullable=True)',
    '    team_type: Mapped[str | None] = mapped_column(String(20), nullable=True)',
    '    color_hex: Mapped[str | None] = mapped_column(String(7), nullable=True)',
    '    ou_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("organizational_units.id"), nullable=True)',
    '    nation_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("nations.id"), nullable=True)',
    '    max_members: Mapped[int | None] = mapped_column(Integer, nullable=True)',
    '    is_persistent: Mapped[bool] = mapped_column(Boolean, default=True)',
    '    exercise_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("exercises.id"), nullable=True)',
]
in_team = False
inserted_team = False
for i, line in enumerate(lines):
    if "class Team(TimestampMixin, Base):" in line:
        in_team = True
        continue
    if in_team and "class " in line:
        break
    if in_team and "tenant_id" in line and "tenants.id" in line:
        for j, new_line in enumerate(team_cols):
            lines.insert(i + 1 + j, new_line)
        inserted_team = True
        print(f"Inserted {len(team_cols)} Team columns at line {i+1}")
        break
if not inserted_team:
    print("WARNING: Could not find Team tenant_id line")

# 4. Find TeamMembership role line and insert new columns after it
tm_cols = [
    '    position: Mapped[str | None] = mapped_column(String(100), nullable=True)',
    '    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)',
]
in_tm = False
inserted_tm = False
for i, line in enumerate(lines):
    if "class TeamMembership(Base):" in line:
        in_tm = True
        continue
    if in_tm and "class " in line:
        break
    if in_tm and 'role: Mapped[str]' in line and 'default="member"' in line:
        for j, new_line in enumerate(tm_cols):
            lines.insert(i + 1 + j, new_line)
        inserted_tm = True
        print(f"Inserted {len(tm_cols)} TeamMembership columns at line {i+1}")
        break
if not inserted_tm:
    print("WARNING: Could not find TeamMembership role line")

result = "\n".join(lines)
MODELS.write_text(result, encoding="utf-8", newline="\n")
print(f"Final lines: {len(lines)}")
print("Phase 1a complete: User/Team/TeamMembership extensions written")