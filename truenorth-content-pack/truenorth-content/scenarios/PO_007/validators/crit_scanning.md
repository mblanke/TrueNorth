# crit_scanning (MUST-PASS) -> QSP critical event: scanning  (EO 007.01)
Pass rule: candidate's submitted technical report identifies the internal service-discovery
event from the pcap/IDS feed (source host = precomp-host foothold, technique T1046, timeframe)
AND does not mis-attribute noise-floor nf-03 (vuln-scanner sweep) as the attack.
```opensearch
# telemetry check: was the inj-01-recon discovery flagged in the candidate's tagged findings index?
GET candidate-findings-*/_search
{ "query": { "bool": { "must": [
  { "match": { "mapped_inject": "inj-01-recon" } },
  { "match": { "candidate_id": "{{CANDIDATE}}" } } ] } } }
```
Fail if zero hits, or if nf-03 is tagged as malicious.