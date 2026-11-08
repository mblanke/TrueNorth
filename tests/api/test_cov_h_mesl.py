"""MESL ingest (app/mesl.py): the spreadsheet an exercise planner uploads.

``parse_mesl`` reads a Master Event Sequence List CSV into serial dicts, tolerating the
column names planners actually use (``Ser``, ``DTG``, ``Inject``, ``Obj``, ... all mean
the same canonical field); ``parse_objectives`` reads the objectives CSV with its own
fallbacks. Both are pure and deterministic, so every test here feeds a real CSV string
and checks the parsed output — no fixtures, no mocks.
"""

from __future__ import annotations

from app.mesl import parse_mesl, parse_objectives


# ── parse_mesl ─────────────────────────────────────────────────────────
def test_canonical_headers_come_through_as_the_twelve_fields():
    rows = parse_mesl(
        "serial,phase,scenario_time,title,description,objective_ref,attack_technique,"
        "delivery_method,from_cell,to_participant,expected_action,moe\n"
        "1,Phase 1,D+0 H+1,Phishing mail,Staff email,T1003.001,credential,"
        "email,Red cell,Blue team,Report the mail,Team reports within 10 min\n"
    )
    assert rows == [
        {
            "serial": 1,
            "phase": "Phase 1",
            "scenario_time": "D+0 H+1",
            "title": "Phishing mail",
            "description": "Staff email",
            "objective_ref": "T1003.001",
            "attack_technique": "credential",
            "delivery_method": "email",
            "from_cell": "Red cell",
            "to_participant": "Blue team",
            "expected_action": "Report the mail",
            "moe": "Team reports within 10 min",
        }
    ]


def test_planner_column_aliases_land_in_the_canonical_field():
    rows = parse_mesl(
        "Ser,Day,DTG,Inject,Detail,Obj,Mitre,Means,Source,Target,Response,Assessment\n"
        "7,2,D+1,Port scan,Nmap sweep,T1046,discovery,radio,OPFOR,White cell,Capture pcap,Hours 2-4\n"
    )
    row = rows[0]
    assert row["serial"] == 7 and row["phase"] == "2" and row["scenario_time"] == "D+1"
    assert row["title"] == "Port scan" and row["description"] == "Nmap sweep"
    assert row["objective_ref"] == "T1046" and row["attack_technique"] == "discovery"
    assert row["delivery_method"] == "radio" and row["from_cell"] == "OPFOR"
    assert row["to_participant"] == "White cell" and row["expected_action"] == "Capture pcap"
    assert row["moe"] == "Hours 2-4"


def test_headers_are_normalised_for_case_spaces_and_padding():
    rows = parse_mesl("  Serial , DAY ,Time\n5,P1,D+0\n")
    assert rows[0]["serial"] == 5 and rows[0]["phase"] == "P1" and rows[0]["scenario_time"] == "D+0"


def test_serials_are_ints_with_a_fallback_to_the_position():
    rows = parse_mesl("serial,title\n007,Zero padded\nA-3,Not a number\n,title only\n")
    assert [r["serial"] for r in rows] == [7, 2, 3]


def test_blank_rows_are_skipped_entirely():
    rows = parse_mesl("serial,title\n1,First\n,,\n,title only\n")
    assert [r["serial"] for r in rows] == [1, 2]


def test_delivery_is_normalised_and_unknown_means_cyber():
    rows = parse_mesl("title,delivery\n" "A,White Cell\n" "B,white-cell\n" "C,EMAIL\n" "D,carrier pigeon\n" "E,network\n")
    assert [r["delivery_method"] for r in rows] == ["white_cell", "white_cell", "email", "cyber", "cyber"]


def test_an_empty_upload_parses_to_nothing():
    assert parse_mesl("") == []


def test_a_row_shorter_than_the_header_leaves_the_missing_fields_empty():
    rows = parse_mesl("serial,title,description\n9,Late columns\n")
    assert rows[0]["title"] == "Late columns" and rows[0]["description"] == ""


def test_unknown_headers_do_not_crash_the_parser():
    rows = parse_mesl("serial,classname\n4,Alpha\n")
    assert rows[0]["serial"] == 4 and rows[0]["title"] == ""


# ── parse_objectives ───────────────────────────────────────────────────
def test_objective_columns_and_their_fallbacks():
    rows = parse_objectives(
        "ref,text,moe,competency_code\n"
        "O1,Detect the phish,Team reports,NICE-SC-T-01\n"
    )
    assert rows == [
        {"ref": "O1", "text": "Detect the phish", "moe": "Team reports",
         "competency_code": "NICE-SC-T-01", "ordinal": 1}
    ]


def test_objective_and_description_headers_are_accepted_for_text():
    rows = parse_objectives("ref,objective\nO1,From objective\n")
    rows2 = parse_objectives("ref,description\nO2,From description\n")
    assert rows[0]["text"] == "From objective" and rows2[0]["text"] == "From description"


def test_a_missing_ref_falls_back_to_obj_then_to_the_position():
    rows = parse_objectives("obj,text\n,First\nOBJ-9,Second\n")
    assert [r["ref"] for r in rows] == ["1", "OBJ-9"]
    assert [r["ordinal"] for r in rows] == [1, 2]


def test_assessment_and_nice_are_accepted_for_moe_and_competency():
    rows = parse_objectives("ref,text,assessment,nice\nO1,Text,Hour 1,NICE-01\n")
    assert rows[0]["moe"] == "Hour 1" and rows[0]["competency_code"] == "NICE-01"


def test_a_row_with_neither_text_nor_ref_is_skipped():
    rows = parse_objectives("ref,text\nO1,Real\n,\n")
    assert [r["ref"] for r in rows] == ["O1"] and len(rows) == 1


def test_an_empty_objectives_upload_parses_to_nothing():
    assert parse_objectives("") == []
