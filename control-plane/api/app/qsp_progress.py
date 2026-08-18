"""Per-learner progress across the QSP qualification spine.

Resolves "where is this learner on the developmental path" by walking the link
that already exists between the CFITES spine and the LMS:

    PerformanceObjective <- CourseModule.po_id -> Course <- Enrollment -> ModuleProgress

Everything is batched: three queries regardless of how many POs are asked about,
so the career map costs the same whether it renders one qualification or the whole
ladder. No LLM, no heuristics — a PO is complete when its module says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from .models import (
    CourseModule,
    Enrollment,
    EnrollmentStatus,
    ModuleProgress,
    ModuleProgressStatus,
)

# Per-PO learner state, in ascending order of "how far along".
NOT_STARTED = "not_started"
IN_PROGRESS = "in_progress"
COMPLETED = "completed"
FAILED = "failed"

# Per-qualification node state on the path.
LOCKED = "locked"
AVAILABLE = "available"
NODE_IN_PROGRESS = "in_progress"
COMPLETE = "complete"


@dataclass
class QualProgress:
    """Aggregate progress for one qualification node."""

    completed: int = 0
    in_progress: int = 0
    total: int = 0

    @property
    def pct(self) -> int:
        return round(100 * self.completed / self.total) if self.total else 0

    def as_dict(self) -> dict:
        return {
            "completed": self.completed,
            "in_progress": self.in_progress,
            "total": self.total,
            "pct": self.pct,
        }


@dataclass
class PathPosition:
    """The single "you are here" marker on the map."""

    qsp_code: str | None = None
    po_code: str | None = None

    def as_dict(self) -> dict:
        return {"current_qsp_code": self.qsp_code, "current_po_code": self.po_code}


@dataclass
class _Node:
    """Internal per-qualification bookkeeping used while deriving node states."""

    qsp_code: str
    dp_order: int
    track: str
    po_states: list[tuple[str, str]] = field(default_factory=list)  # (po_code, state)


def po_progress(
    db: Session,
    user_id,
    po_ids: list,
    modules_by_po: dict[str, CourseModule] | None = None,
) -> dict[str, str]:
    """Map po_id -> learner state for one user. Three queries, no per-PO work.

    A PO with no course module, no enrolment, or no progress row is `not_started` —
    the spine is authored well ahead of anyone training against it, so "unknown"
    and "not started" are the same thing from the learner's point of view.

    Pass `modules_by_po` when the caller already loaded the PO -> module mapping
    to drop this to two queries.
    """
    if not po_ids or user_id is None:
        return {}

    if modules_by_po is None:
        # A PO owns one module in practice; if authoring ever produces more, the
        # lowest ordinal is the one the path follows.
        modules = sorted(
            db.query(CourseModule).filter(CourseModule.po_id.in_(po_ids)).all(),
            key=lambda m: m.ordinal,
        )
        modules_by_po = {}
        for module in modules:
            modules_by_po.setdefault(str(module.po_id), module)
    if not modules_by_po:
        return {}
    module_by_po = modules_by_po

    course_ids = {m.course_id for m in module_by_po.values()}
    enrollments = (
        db.query(Enrollment)
        .filter(Enrollment.user_id == user_id, Enrollment.course_id.in_(course_ids))
        .all()
    )
    if not enrollments:
        return {}
    enroll_by_course = {str(e.course_id): e for e in enrollments}

    progress_rows = (
        db.query(ModuleProgress)
        .filter(
            ModuleProgress.enrollment_id.in_([e.id for e in enrollments]),
            ModuleProgress.module_id.in_([m.id for m in module_by_po.values()]),
        )
        .all()
    )
    progress_by_module = {str(p.module_id): p for p in progress_rows}

    out: dict[str, str] = {}
    for po_id, module in module_by_po.items():
        enrollment = enroll_by_course.get(str(module.course_id))
        if enrollment is None:
            continue
        progress = progress_by_module.get(str(module.id))
        out[po_id] = _resolve_state(progress, enrollment, module)
    return out


def _resolve_state(
    progress: ModuleProgress | None, enrollment: Enrollment, module: CourseModule
) -> str:
    """Prefer the module-level record; fall back to the enrolment when it is absent."""
    if progress is not None:
        if progress.status == ModuleProgressStatus.completed:
            # `completed` records the attempt, not the verdict — the module's own
            # pass_threshold decides whether it counts as a pass.
            pass_mark = (progress.max_score or 100) * ((module.pass_threshold or 70) / 100)
            return FAILED if progress.score < pass_mark else COMPLETED
        if progress.status == ModuleProgressStatus.in_progress:
            return IN_PROGRESS
        if progress.status == ModuleProgressStatus.skipped:
            return NOT_STARTED
        # not_started, but an attempt was recorded -> the learner has opened it.
        return IN_PROGRESS if progress.attempts else NOT_STARTED

    if enrollment.status == EnrollmentStatus.completed:
        return COMPLETED
    if enrollment.status == EnrollmentStatus.failed:
        return FAILED
    if enrollment.status == EnrollmentStatus.in_progress:
        return IN_PROGRESS
    return NOT_STARTED


# A node is (qsp_code, dp_order, track, [(po_code, po_state), ...]) — the shape both
# state derivation and the position marker work from.
Node = tuple[str, int, str, list[tuple[str, str]]]


def aggregate(po_states: list[tuple[str, str]]) -> QualProgress:
    """Roll one qualification's per-PO states up into its counts."""
    agg = QualProgress(total=len(po_states))
    for _, state in po_states:
        if state == COMPLETED:
            agg.completed += 1
        elif state in (IN_PROGRESS, FAILED):
            agg.in_progress += 1
    return agg


def node_states(nodes: list[Node], edges: list[tuple[str, str]]) -> dict[str, str]:
    """Derive each qualification's path state from its progress and its prerequisites.

    A node unlocks when every node pointing at it is complete; entry nodes (nothing
    pointing at them) are always available.
    """
    prereqs: dict[str, list[str]] = {code: [] for code, _, _, _ in nodes}
    for src, dst in edges:
        if dst in prereqs:
            prereqs[dst].append(src)

    # Complete/in-progress come from the learner's own record; locked/available
    # depend on upstream nodes, so settle the self-evident states first.
    out: dict[str, str] = {}
    for code, _, _, po_states in nodes:
        agg = aggregate(po_states)
        if agg.total and agg.completed == agg.total:
            out[code] = COMPLETE
        elif agg.completed or agg.in_progress:
            out[code] = NODE_IN_PROGRESS
        else:
            out[code] = ""

    for code, _, _, _ in nodes:
        if out[code]:
            continue
        blocked = any(out.get(src) != COMPLETE for src in prereqs.get(code, []))
        out[code] = LOCKED if blocked else AVAILABLE
    return out


def current_position(nodes: list[Node], states: dict[str, str]) -> PathPosition:
    """Pick the single "you are here" marker.

    The furthest node the learner has actually touched, and within it the first PO
    that is not yet complete. Falls back to the first available node when nothing
    has been started, so a brand-new learner still sees a starting point.
    """
    touched = [n for n in nodes if states.get(n[0]) in (NODE_IN_PROGRESS, COMPLETE)]
    if touched:
        # Furthest by DP, then by how much of it is done — a learner mid-DP2 is
        # ahead of one who only just finished DP1.
        code, _, _, po_states = max(
            touched,
            key=lambda n: (n[1], sum(1 for _, s in n[3] if s == COMPLETED)),
        )
        for po_code, state in po_states:
            if state != COMPLETED:
                return PathPosition(qsp_code=code, po_code=po_code)
        # Node fully complete — point at whatever has just opened up.
        nxt = _first_available(nodes, states)
        return nxt if nxt.qsp_code else PathPosition(qsp_code=code, po_code=None)

    return _first_available(nodes, states)


def _first_available(nodes: list[Node], states: dict[str, str]) -> PathPosition:
    """Earliest unlocked node, preferring the rank ladder — a specialty is an opt-in
    fork, so it should never be suggested over the core progression at the same DP."""
    ordered = sorted(nodes, key=lambda n: (n[1], 1 if n[2] == "specialty" else 0, n[0]))
    for code, _, _, po_states in ordered:
        if states.get(code) == AVAILABLE:
            return PathPosition(qsp_code=code, po_code=po_states[0][0] if po_states else None)
    return PathPosition()
