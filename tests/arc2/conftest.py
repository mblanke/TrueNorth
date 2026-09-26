"""Shared fixture for the ARC² contract tests: a tiny repo root with a four-row crosswalk, one
course that already delivers PO_009, and the range the fixture manifest reuses."""

from __future__ import annotations

from pathlib import Path

import pytest
from arc2 import check

CROSSWALK = """qsp_code,nqual,tier,po_id,po_title,eos,conditions,critical_events,assessment_type,duration_min,pass_standard,deliverable,environment,target_role,nice_dcwf_task,component_version,scenario_count,build_hours,status
ALJQ,ALJQ,core,PO_007,Analyze Malicious Activity in Network Traffic,007.01,pcap,scanning;exfiltration;lateral_movement,PC practical,240,P/F,report,COTE,Cyber Defense Analyst,T0023,SP800-181r1,4,200,todo
ALJQ,ALJQ,core,PO_009,Security Monitoring,009.01,siem,alert_triage;escalation,PC practical,180,P/F,report,COTE,Cyber Defense Analyst,T0023,SP800-181r1,2,80,todo
ALJQ,ALJQ,core,PO_010,Example,010.01,siem,example_event,PC practical,60,P/F,report,COTE,Analyst,T0023,SP800-181r1,1,10,example
TEMP64,TEMP64,core,PO_001-003,Gain Access,001,lab,initial_access,PC practical,240,P/F,report,COTE,Operator,T0028,SP800-181r1,3,100,offensive_author
"""

CLAIMING_COURSE = """course_code: C204
title: Security Monitoring
modules:
- ordinal: 1
  title: SIEM
  po:
    qsp_code: ALJQ
    po_code: PO_009
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / check.CROSSWALK_REL).parent.mkdir(parents=True)
    (root / check.CROSSWALK_REL).write_text(CROSSWALK)
    (root / check.COURSES_REL).mkdir(parents=True)
    (root / check.COURSES_REL / "c204.yaml").write_text(CLAIMING_COURSE)
    (root / "content" / "ranges" / "soc-training").mkdir(parents=True)  # the range full_manifest() reuses
    return root
