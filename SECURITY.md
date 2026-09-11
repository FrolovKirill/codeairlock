# Security policy

## Supported version

This public snapshot supports only the versions pinned in the Dockerfiles and
`vendor/assets.lock.json`. Upgrading a principal component requires new functional,
network-policy, retry, and licensing review.

## Reporting a vulnerability

Use the private security-reporting channel configured by the repository maintainer.
If the eventual hosting platform has no private channel, ask the maintainer to add
one before disclosing credentials, repository text, endpoint names, or exploit
details. A public issue is suitable for non-sensitive hardening suggestions only.

## Deployment guidance

- Treat the host, Docker daemon, browser, model server, embedding server, selected
  container images, and optional search service as trusted.
- Keep the UI bound to `127.0.0.1`. For remote use, place an authenticated,
  encrypted access layer on the trusted host instead of changing the generated
  Compose port to a public bind.
- Keep `.env`, `runtime/`, downloaded artifacts, diagnostics, and logs out of Git.
- Treat the loopback project manager as a trusted host process with Docker access.
  Never expose it through a public bind or forward its bearer token. It is not
  reachable from the isolated workstation and must remain outside that namespace.
- Keep `runtime/projects.json` and the per-project Docker volumes together when
  backing up; the registry binds a folder to its saved session history.
- Keep project trees free of host control sockets, FIFOs and devices. Registration
  and pre-mount checks reject them, but a trusted host can create new entries after
  validation. Read-only mounts do not block Unix socket connections; nftables does
  not filter pathname Unix IPC. This design does not isolate a hostile host.
- Use a repository checkout that contains no unrelated secrets. Read-only mounting
  prevents writes; it does not stop Kilo from reading files and sending selected
  context to the trusted model or embedding endpoint.
- Review every Kilo shell or external-directory prompt. Review every proposed
  search query on the host; approval discloses that exact query externally.
- Run `./codeairlock check` and `./codeairlock audit` after changes to Docker, network,
  endpoint, or host configuration.

The Kilo permission system is a user interface control. The kernel-enforced
container and nftables boundaries are the isolation mechanism. Chromium runs with
`--no-sandbox` inside that outer boundary. No configuration here protects against
a compromised host kernel or Docker daemon.
