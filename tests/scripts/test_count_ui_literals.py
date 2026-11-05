"""scripts/count_ui_literals.py: the #436 literal inventory as a ratchet.

Three layers:
  * the per-file figures the #436 inventory reported at eec5390 are pinned here
    and re-counted from that commit, so the counter and the inventory cannot
    drift apart silently. Three files differ from the #436 table and are pinned
    at the re-counted value instead — each carries a comment saying why;
  * small committed fixture templates under tests/scripts/fixtures/ pin the
    scanner policies (what counts as A/B/C and what does not) without dragging
    the real components into tests;
  * the --check ratchet is exercised against a throwaway baseline.

Reconciliation with the #436 return (which printed A=447 B=61 C=217): the
#436 per-file table sums to A=427, not 447 — its own totals line was already
inconsistent with its per-file figures. This counter prints A=426 B=62 C=218:
B is +1 over the per-file table (single-quoted attribute values now count —
detection-panel.component.ts:68 `placeholder='...:"..."'`, see the pinned
comment), C matches it except approval-queue (-2, state keys) and schedule
(+3), and A is -1 on detection-panel (see the pinned comments).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("count_ui_literals", ROOT / "scripts" / "count_ui_literals.py")
cui = importlib.util.module_from_spec(_spec)
sys.modules["count_ui_literals"] = cui
_spec.loader.exec_module(cui)

# The #436 inventory (TN10X-F6-literal-inventory), per-file figures at
# eec5390677b58bfece641698ae84d57d2d941126. #436 printed totals A=447 B=61 C=217,
# but its per-file table sums to A=427 — see the module docstring.
FIGURES_AT_EEC5390: dict[str, tuple[int, int, int]] = {
    "features/login/login.component.ts": (5, 1, 0),
    "features/classes/my-classes.component.ts": (30, 10, 18),
    "features/exercise-detail/exercise-detail.component.ts": (26, 2, 13),
    # re-pinned 14 -> 13: every letter-bearing A source at eec5390 is accounted
    # for (9 text segments incl. the field:value help line, 'Checking…'/'Submit'
    # before the pipe, '—' fragments excluded as letterless); the #436 14th is
    # not recoverable (transcript lost).
    # re-pinned B 0 -> 1 (fix round r2): the static-B scan now accepts single-
    # quoted attribute values, so the user-facing placeholder at
    # detection-panel.component.ts:68 counts (placeholder='process.name:
    # "powershell.exe" AND ...' — the only single-quoted B attribute in the 15
    # journey files, so no other pin moves).
    "features/exercise-detail/detection-panel.component.ts": (13, 1, 8),
    "features/labs/lab-page.component.ts": (10, 0, 18),
    "shared/components/vm-console/tn-vm-console.component.ts": (9, 1, 7),
    "shared/components/vm-console/console-state.ts": (0, 0, 22),
    # re-pinned 50 -> 53: all 53 are rendered words (capacity row labels
    # row('vCPU'|'RAM'|'Disk'), 'OK' snackbar action x4, booking snackbar
    # titles/notes, templateName/personName/stateNote fallbacks). Dropping any
    # would remove a genuinely rendered word; which three fragments #436
    # excluded is not recoverable (transcript lost).
    "features/schedule/schedule.component.ts": (135, 7, 53),
    # re-pinned 5 -> 3: the two extra are the Open.mode state keys 'propose'/
    # 'reject', which the template only compares against and never renders
    # (the buttons carry their own literal labels). Policy: state keys don't count.
    "features/schedule/approval-queue.component.ts": (32, 0, 3),
    "features/classes/classes.component.ts": (75, 6, 36),
    "features/classes/roll-call-domains-card.component.ts": (7, 1, 5),
    "features/classes/class-live.component.ts": (16, 3, 3),
    "features/classes/class-closeout.component.ts": (30, 8, 12),
    "features/scoring/scoring.component.ts": (20, 1, 7),
    "shared/components/range-credentials/range-credentials.component.ts": (18, 21, 13),
}

EEC5390 = "eec5390677b58bfece641698ae84d57d2d941126"


@pytest.mark.parametrize("rel,expected", sorted(FIGURES_AT_EEC5390.items()))
def test_matches_the_436_inventory_at_eec5390(rel: str, expected: tuple[int, int, int]) -> None:
    src = cui._read(rel, EEC5390)
    a = b = 0
    tspan = cui.template_span(src)
    if tspan:
        a, b = cui.count_ab(src[tspan[0] : tspan[1]])
    c = cui.classify(cui.count_c(src))
    assert (a, b, c) == expected, f"{rel}: got A={a} B={b} C={c}, want {expected}"


def test_totals_match_the_reconciled_436_inventory() -> None:
    per_file = cui.counts(EEC5390)
    tot = cui._totals(per_file)
    # Sum of the per-file table above. #436's printed A=447 C=217 disagree with
    # its own per-file sums (427 / 217); see the module docstring for the -2
    # approval-queue, +3 schedule, -1 A and +1 B (single-quoted attribute)
    # detection-panel reconciliation.
    assert tot == {"A": 426, "B": 62, "C": 218}


# --- scanner policies, pinned on committed fixtures -------------------------

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class TestCommittedFixtures:
    """The policies above, exercised over tests/scripts/fixtures/."""

    def test_template_basics_ab(self) -> None:
        src = _fixture("template_basics.ts")
        tspan = cui.template_span(src)
        a, b = cui.count_ab(src[tspan[0] : tspan[1]])
        # A: 'Sign in to the range' | 'A single range' + 'One shared range' +
        #    'Bare case label' (bare @case: only the label is skipped, no paren
        #    scan runs) | 'Hello' + 'world' | 'Required field' (before the
        #    pipe) | 'Q&A' = 8; comment, mat-icon/style bodies, date-pipe arg
        #    and the letterless segment contribute nothing.
        # B: placeholder + title, plus 'Required' from [placeholder] = 3;
        #    [attr.aria-label] (no fragment), class/click/routerLink ignored.
        assert (a, b) == (8, 3)

    def test_ts_literals_c(self) -> None:
        c = cui.classify(cui.count_c(_fixture("ts_literals.ts")))
        # 'Your range could not be built.' | 'Class' + 'saved' + 'created' |
        # 'forfeited' | 'min' | 'RAM' = 7; imports, urls, gsap, media/calc
        # strings, comments, key lookups and the template/styles spans do not.
        assert c == 7


def _ab(template: str) -> tuple[int, int]:
    return cui.count_ab(template)


class TestClassATemplateText:
    def test_text_segments_and_interpolation_separators(self) -> None:
        # Two text segments; {{ ch }} separates and contributes nothing.
        assert _ab("<div>Hello {{ ch }} world</div>") == (2, 0)

    def test_tag_bodies_mat_icon_and_style_skipped(self) -> None:
        t = "<mat-icon>login</mat-icon><style>.x{}</style><p>Sign in</p>"
        assert _ab(t) == (1, 0)

    def test_comments_skipped(self) -> None:
        assert _ab("<!-- Hidden <p>words</p> --><span>Visible</span>") == (1, 0)

    def test_control_flow_headers_skipped_but_branch_text_counts(self) -> None:
        t = "@if (mode === 'individual') { A single range } @else { One shared range }"
        assert _ab(t) == (2, 0)

    def test_quoted_fragments_in_interpolation_count_except_after_pipe(self) -> None:
        assert _ab("{{ cond ? 'individual' : 'team' }}") == (2, 0)
        assert _ab("{{ started | date:'mediumDate' }}") == (0, 0)
        # 'HH:mm' sits before no pipe only in the first segment here:
        assert _ab("{{ (ended | date:'HH:mm') }}") == (0, 0)

    def test_entities_dropped_and_punctuation_only_ignored(self) -> None:
        assert _ab("<p>Q&amp;A</p>") == (1, 0)
        assert _ab("<p>// -- ...</p>") == (0, 0)

    def test_segment_must_contain_a_letter(self) -> None:
        assert _ab("<p>42 · 100%</p>") == (0, 0)

    def test_control_header_interpolation_does_not_rescan_as_text(self) -> None:
        # _skip_parens must resume past '}}', not on it: re-scanning would read
        # the braces as text, and an absent '}}' returned -1, i.e. template[-1],
        # restarting the scan and double-counting the whole template.
        assert cui._skip_parens("@if {{ cond }} (x) {", 4) == 14  # past '}}', not on it
        assert _ab("@if {{ x (unclosed no-close") == (0, 0)  # was (1, 0) via the -1 restart


class TestClassBLiteralAttributes:
    def test_static_values(self) -> None:
        t = '<input placeholder="e.g. medical appointment" title="Copy password">'
        assert _ab(t) == (0, 2)

    def test_static_single_quoted_value_counts(self) -> None:
        # detection-panel.component.ts:68 — a single-quoted value whose own
        # double quotes must not end it (the r1 scanner matched no form here).
        t = """<textarea placeholder='process.name:"powershell.exe" AND ...'>"""
        assert _ab(t) == (0, 1)

    def test_binding_single_quoted_value_counts(self) -> None:
        t = "<input [placeholder]='warn ? \"Required\" : \"\"'>"
        assert _ab(t) == (0, 1)

    def test_binding_fragments(self) -> None:
        t = """<input [placeholder]="warn ? 'Required' : ''" [attr.aria-label]="lbl">"""
        assert _ab(t) == (0, 1)

    def test_unrelated_attributes_ignored(self) -> None:
        t = '<div class="hero" (click)="login()" [routerLink]="[\'/dashboard\']"></div>'
        assert _ab(t) == (0, 0)


class TestClassCTsLiterals:
    def test_prose_counts_and_code_words_do_not(self) -> None:
        src = """
const url = '/dashboard';
import gsap from 'gsap';
const label = STATUS_LABEL[status]; // key lookup, not a literal
const msg = 'Your range could not be built.';
const ease = 'power3.out';
"""
        assert cui.classify(cui.count_c(src)) == 1

    def test_backtick_interpolation_fragments(self) -> None:
        src = "const m = `Class ${c.name} ${f.id ? 'saved' : 'created'}`;"
        assert cui.classify(cui.count_c(src)) == 3

    def test_comments_and_styles_and_template_skipped(self) -> None:
        src = """
// a 'quoted' remark
/* block 'quote' */
@Component({ template: `<p>Template words</p>`, styles: [`p { color: red; }`] })
export class X {}
"""
        assert cui.classify(cui.count_c(src)) == 0

    def test_css_and_media_query_strings_do_not_count(self) -> None:
        src = "const q = '(min-width: 1024px)'; const w = 'calc(100% - 64px)';"
        assert cui.classify(cui.count_c(src)) == 0


class TestRatchet:
    @pytest.fixture()
    def baseline_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        p = tmp_path / ".dod-ui-literals-baseline"
        monkeypatch.setattr(cui, "BASELINE_FILE", p)
        return p

    def test_report_prints_per_file_lines_and_a_total(self, capsys: pytest.CaptureFixture) -> None:
        assert cui.main(["--report"]) == 0
        out = capsys.readouterr().out
        assert "student/features/login/login.component.ts: A=" in out
        final = out.strip().splitlines()[-1]
        assert final.startswith("total: A=") and "sum=" in final

    def test_git_alone_counts_a_past_state_end_to_end(self, capsys: pytest.CaptureFixture) -> None:
        # --git without --check: prints and exits 0, never touches a baseline.
        assert cui.main(["--git", EEC5390]) == 0
        out = capsys.readouterr().out
        assert "total: A=426 B=62 C=218 sum=706" in out
        assert "baseline" not in out

    def test_check_bootstraps_when_baseline_missing(self, baseline_file: Path) -> None:
        # The harness writes the baseline; until then --check passes at base=total
        # and leaves the file absent (only a falling total writes one).
        assert not baseline_file.exists()
        assert cui.main(["--check"]) == 0
        assert not baseline_file.exists()

    def test_check_fails_when_total_rises(self, baseline_file: Path, capsys: pytest.CaptureFixture) -> None:
        baseline_file.write_text("0\n")
        assert cui.main(["--check"]) == 1
        assert "UI-LITERALS FAIL" in capsys.readouterr().err
        assert baseline_file.read_text().strip() == "0"

    def test_check_passes_and_lowers_when_total_falls(self, baseline_file: Path) -> None:
        per_file = cui.counts(None)
        current = sum(int(v[k]) for v in per_file.values() for k in ("A", "B", "C"))
        baseline_file.write_text(f"{current + 1}\n")
        assert cui.main(["--check"]) == 0
        assert baseline_file.read_text().strip() == str(current)

    def test_check_refuses_git_rev(self, baseline_file: Path) -> None:
        # A past-state count must never move the live baseline (fix round r2:
        # --check --git REV used to lower it when the historical total was below).
        baseline_file.write_text("99999\n")
        with pytest.raises(SystemExit) as exc:
            cui.main(["--check", "--git", EEC5390])
        assert exc.value.code == 2
        assert baseline_file.read_text().strip() == "99999"
