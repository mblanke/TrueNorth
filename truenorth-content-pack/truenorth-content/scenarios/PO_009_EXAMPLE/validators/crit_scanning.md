# crit_scanning (MUST-PASS) -> QSP critical event: scanning
Pass rule: candidate's submitted report identifies the internal service-discovery event
(source host, technique, timeframe) AND does not mis-attribute noise-floor nf-03 as the attack.
```opensearch
# telemetry check: were the inj-01 discovery artifacts flagged in the candidate's tagged findings index?
GET candidate-findings-*/_search
{ "query": { "bool": { "must": [
  { "match": { "mapped_inject": "inj-01-recon" } },
  { "match": { "candidate_id": "{{CANDIDATE}}" } } ] } } }
```
Fail if zero hits, or if nf-03 is tagged as malicious.
