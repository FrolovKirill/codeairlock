# Public-release validation

This file records checks performed on the source-only public release. It contains
no private endpoint, repository, prompt, response, or runtime data.

## Scope

- Static inventory and secret-pattern review of the public directory.
- Python unit and fault-injection tests using loopback sockets and synthetic data.
- Python bytecode compilation of launcher, scripts, tests, deployment helpers, and
  the synthetic repository.
- Git ignore and fresh-repository boundary checks.

No real model server, embedding server, SearxNG instance, private repository, or
existing CodeAirlock deployment is used. No claim is made for model quality,
benchmark reliability, clean-Linux compatibility, or x86-64 compatibility.

## Results on 2026-09-11

- `python3 -m compileall -q codeairlock deploy scripts tests demo-repo` completed
  successfully with bytecode redirected outside the repository.
- `python -m unittest discover -s tests -v` ran in an existing Python 3.11
  container with `--network none`, a read-only root filesystem, a temporary `/tmp`,
  and read-only mounts of only this release's `scripts/` and `tests/`. All 25 tests
  passed.
- `tests/sdk_retry_smoke.py` ran in the existing pinned Kilo 7.5.16 workstation
  image with `--network none`, a read-only root filesystem, temporary `/tmp` and
  home filesystems, no repository mount, and read-only public `scripts/` and
  `tests/` mounts. Kilo surfaced the terminal synthetic failure after exactly three
  gateway attempts (two retries), with no client-side retry multiplication.
- The committed asset manifest parsed successfully, contained exactly the two
  expected ARM64 assets, used HTTPS GitHub URLs, and contained 64-character
  lowercase SHA-256 values.
- The file inventory contained no `.env`, runtime directory, downloaded VSIX or
  tarball, partial download, log, bytecode cache, attachment, or parent Git history.
- The directory was initialized as an independent Git repository on `main`, with
  no parent history or remote. The publication snapshot contains only the reviewed
  source inventory.
- Independent release review found two documentation inaccuracies in retry-budget
  and partial-response failure handling; both descriptions were corrected without
  changing the tested gateway behavior. The review found no source-publication
  blocker; it does not constitute an exhaustive security audit.
- Demo and connected configuration generation passed in a temporary directory with
  Docker execution replaced by a synthetic stub. Assertions covered localhost UI
  binding, a read-only repository mount, dropped capabilities, absence of a Docker
  socket mount, disabled search by default, and generated default-deny chains with
  the early DNS block. This checks generated configuration, not kernel enforcement.
- The exact Kilo `v7.5.16` and code-server `v4.136.2` upstream license files were
  retrieved from their public GitHub tags and both identified as MIT.
- The public name and executable were changed to CodeAirlock / `codeairlock`.
  Launcher help, Python and JSON parsing, and synthetic configuration generation
  passed after the rename. Generated Compose and Docker image names match the
  launcher's build tags. No stale project-name references remain in the source
  inventory. Gateway and network-policy behavior were unchanged.

The first host-side unit-test attempt could not bind loopback sockets because of
the invoking filesystem/process sandbox and therefore ran zero tests. The isolated
network-disabled container run above is the successful test result. No project
runtime container, Compose stack, model endpoint, or repository mount was started.
