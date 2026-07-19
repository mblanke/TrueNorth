# crit_exfil (MUST-PASS) -> critical event: exfiltration
Pass: report identifies staging + outbound exfil (inj-03) and correctly excludes backup nf-01.
```opensearch
GET candidate-findings-*/_search
{ "query": { "match": { "mapped_inject": "inj-03-exfil" } } }
```
