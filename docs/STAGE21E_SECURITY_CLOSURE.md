# Stage 21e — Security closure

Two findings from the Stage 20 report are closed, and the report no longer lists them as open.

## INJ-09: hardening is now the default
Log and tool text that looks like prompt injection is removed (and the rest fenced as data) **by default** in `build_prompt` and the ReAct tool loop,
not only inside the pipeline. Opt out with `build_prompt(..., harden=False)` or `with hardened(False):` (used only to measure the unhardened behaviour).
Dev-split diagnosis scores did not change (the gate's baseline shows identical numbers). The attack now turns `vulnerable` (fails CI) if the default is ever switched off.

## APP-08: request-store integrity chain
`RequestStore(root, clock, seal_key=KEY)` chains every event with HMAC-SHA256 (request id + previous seal + event). A line written into `journal.jsonl`
without the key, an edited, dropped, reordered or transplanted line, or a wrong key raises `StoreTampered`. Appends to a broken chain are refused; the controller skips a tampered
request, so its run is never delivered an approval and expires closed. Without a key the store behaves as before (tests keep the old behaviour as a negative control).
Limit: a same-user attacker who can read the key can still forge; run the gate and the worker as different OS users to close that.

## Still open (honest)
INJ-04 (plausible fake log lines, semantic) and INJ-07 (known pattern evasions). Real Docker selftest is run on your machine: see the Stage 16 docs.

## Check
```
python -m pytest tests\test_integrity.py tests\test_security.py -q
python -m incident_agent.agenteval run
python -m incident_agent.security report
```
