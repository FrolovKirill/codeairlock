# Contributing

Keep changes small enough that the security boundary remains reviewable. Do not
commit `.env` files, runtime state, repository samples copied from real projects,
model responses, diagnostics, downloaded release artifacts, or logs.

Use only synthetic fixtures in tests. Unit tests must not call public networks or a
real model. Run:

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q codeairlock deploy scripts tests demo-repo
```

Changes to routes, network policy, mounts, container capabilities, retry behavior,
or approval handling need matching fault-injection tests. A dependency change also
needs reviewed upstream provenance, updated checksums where applicable, an updated
third-party inventory, and an SBOM/license audit of the exact rebuilt image.

Do not add a generic outbound proxy, runtime DNS, automatic search approval,
redirect following, remote media fetches, or retries after an ambiguous POST or a
partially exposed response without documenting and testing the changed threat
model.
