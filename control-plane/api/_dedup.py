import pathlib

MODELS = pathlib.Path(r"D:\Projects\Dev\TrueNorth Range\Core_Docs\control-plane\api\app\models.py")
content = MODELS.read_text(encoding="utf-8")
lines = content.splitlines()
print(f"Before dedup: {len(lines)} lines")

# Remove duplicate Float import
float_count = 0
new_lines = []
for line in lines:
    if line.strip() == "Float,":
        float_count += 1
        if float_count > 1:
            continue
    new_lines.append(line)
lines = new_lines
print(f"Float imports removed: {float_count - 1}")

# Remove duplicate User extended fields (second block)
# Find the pattern: two consecutive "# -- Extended identity fields" blocks
result = []
skip_until_tenant = False
seen_extended = False
for i, line in enumerate(lines):
    if "# -- Extended identity fields" in line:
        if seen_extended:
            # This is the duplicate block - skip until we hit tenant relationship
            skip_until_tenant = True
            continue
        seen_extended = True
    if skip_until_tenant:
        if "tenant: Mapped[Tenant] = relationship" in line:
            skip_until_tenant = False
            result.append(line)
        continue
    result.append(line)
lines = result

# Remove duplicate Team fields
result = []
seen_desc = False
skip_team_dupes = False
in_team_class = False
for i, line in enumerate(lines):
    if "class Team(TimestampMixin, Base):" in line:
        in_team_class = True
        seen_desc = False
    if in_team_class and "class TeamMembership" in line:
        in_team_class = False
    if in_team_class and "description: Mapped[str | None]" in line:
        if seen_desc:
            skip_team_dupes = True
            continue
        seen_desc = True
    if skip_team_dupes and in_team_class:
        if "exercise_id: Mapped[uuid.UUID | None]" in line:
            skip_team_dupes = False
            continue  # skip the duplicate exercise_id too
        continue
    result.append(line)
lines = result

# Remove duplicate TeamMembership fields
result = []
seen_position = False
in_tm_class = False
for i, line in enumerate(lines):
    if "class TeamMembership(Base):" in line:
        in_tm_class = True
        seen_position = False
    if in_tm_class and line.startswith("# --") or (in_tm_class and line.startswith("class ")):
        if "class TeamMembership" not in line:
            in_tm_class = False
    if in_tm_class and "position: Mapped[str | None]" in line:
        if seen_position:
            continue
        seen_position = True
    if in_tm_class and "joined_at: Mapped[datetime | None]" in line:
        if seen_position and result and "position: Mapped[str | None]" not in result[-1]:
            continue
    result.append(line)
lines = result

MODELS.write_text("\n".join(lines), encoding="utf-8", newline="\n")
print(f"After dedup: {len(lines)} lines")