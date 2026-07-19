# crit_lateral (MUST-PASS) -> QSP critical event: lateral_movement  (EO 007.02)
Pass: report identifies DC -> file-server movement (inj-02) from the pcap with who/what/where/when
(SMB/RPC session from foothold to srv2019) and nf-02 (admin remote PowerShell) is not mis-flagged
as the attack.
```opensearch
GET candidate-findings-*/_search
{ "query": { "match": { "mapped_inject": "inj-02-lateral" } } }
```
Fail if zero hits, or if nf-02 is tagged as malicious.