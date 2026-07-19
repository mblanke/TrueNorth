# crit_lateral (MUST-PASS) -> critical event: lateral_movement
Pass: report identifies DC->file-server movement (inj-02) with who/what/where/when; nf-02 not
mis-flagged as attack.
```opensearch
GET candidate-findings-*/_search
{ "query": { "match": { "mapped_inject": "inj-02-lateral" } } }
```
