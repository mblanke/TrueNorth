# crit_exfil (MUST-PASS) -> QSP critical event: exfiltration  (EO 007.03)
Pass: report identifies staging + outbound exfil (inj-03, T1048 over an alternate protocol) from
the pcap/IDS feed and correctly excludes backup nf-01.
```opensearch
GET candidate-findings-*/_search
{ "query": { "match": { "mapped_inject": "inj-03-exfil" } } }
```
Fail if zero hits, or if nf-01 (nightly backup) is tagged as the exfil event.