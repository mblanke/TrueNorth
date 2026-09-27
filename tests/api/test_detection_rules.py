"""Detection rules: create through the API.

Nothing tested the create route, which is how it shipped raising TypeError on every
call: `DetectionRule(**data, author=...)` with `author` also in the request body.
"""

SIGMA = """title: Suspicious PowerShell Download Cradle
logsource:
  category: process_creation
  product: windows
detection:
  selection:
    CommandLine|contains: DownloadString
  condition: selection
"""


def _rule(**extra):
    return {"title": "PowerShell download cradle", "detection_yaml": SIGMA, "level": "high", **extra}


def test_a_rule_can_be_created(client):
    resp = client.post("/detection-rules", json=_rule())
    assert resp.status_code == 201, resp.text
    assert resp.json()["title"] == "PowerShell download cradle"


def test_the_author_is_the_caller_not_the_body(client):
    resp = client.post("/detection-rules", json=_rule(author="someone else entirely"))
    assert resp.status_code == 201, resp.text
    assert resp.json()["author"] != "someone else entirely"


def test_a_rule_without_a_condition_is_rejected(client):
    bad = SIGMA.replace("  condition: selection\n", "")
    resp = client.post("/detection-rules", json=_rule(detection_yaml=bad))
    assert resp.status_code == 422
