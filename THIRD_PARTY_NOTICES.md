# Third-party software and redistribution notes

The repository's original glue code and documentation are offered under the MIT
license in `LICENSE`. That license does not replace the licenses of software that
the build downloads or installs.

This inventory was checked against upstream metadata for the versions named below
on 2026-09-11. It is a practical release checklist, not legal advice or a guarantee
that every transitive package has been classified.

## Principal pinned components

| Component | Version | Upstream license indication | How it enters the image |
| --- | --- | --- | --- |
| [Kilo Code](https://github.com/Kilo-Org/kilocode/tree/v7.5.16) | 7.5.16 | [MIT at the pinned tag](https://github.com/Kilo-Org/kilocode/blob/v7.5.16/LICENSE) | A release VSIX is downloaded locally from the exact URL and SHA-256 in `vendor/assets.lock.json`. |
| [code-server](https://github.com/coder/code-server/releases/tag/v4.136.2) | 4.136.2 | [MIT](https://github.com/coder/code-server/blob/v4.136.2/LICENSE); upstream also ships [third-party notices](https://github.com/coder/code-server/blob/v4.136.2/ThirdPartyNotices.txt) | A release tarball is downloaded locally from the exact URL and SHA-256 in `vendor/assets.lock.json`. |
| [LanceDB Node.js](https://github.com/lancedb/lancedb/tree/v0.26.2) | 0.26.2 | The pinned [package metadata](https://github.com/lancedb/lancedb/blob/v0.26.2/nodejs/package.json) declares Apache-2.0; see the upstream [license](https://github.com/lancedb/lancedb/blob/v0.26.2/LICENSE). | Installed from npm while building the workstation image. Native optional packages and transitive dependencies carry their own licenses. |
| [TypeScript](https://github.com/microsoft/TypeScript/tree/v5.9.3) | 5.9.3 | [Apache-2.0](https://github.com/microsoft/TypeScript/blob/v5.9.3/LICENSE.txt) | Installed from npm while building the workstation image. |
| [TypeScript Language Server](https://github.com/typescript-language-server/typescript-language-server/tree/v6.0.0) | 6.0.0 | The pinned [package metadata](https://github.com/typescript-language-server/typescript-language-server/blob/v6.0.0/package.json) declares Apache-2.0. | Installed from npm while building the workstation image. |
| [Pyright](https://github.com/microsoft/pyright/tree/1.1.414) | 1.1.414 | [MIT](https://github.com/microsoft/pyright/blob/1.1.414/LICENSE.txt) | Installed from npm while building the workstation image. The npm wrapper/platform package may have separate metadata. |

Kilo's community branding guidance asks derivative integrations to avoid implying
official status and recommends the form “Project for Kilo” when Kilo appears in a
product name. This repository uses Kilo only to describe compatibility, includes
no Kilo logo, and claims no affiliation. See the official [Kilo ecosystem branding
guidance](https://kilo.ai/docs/contributing/ecosystem). Trademark rights are
separate from the MIT copyright license.

## Images and distribution packages

The Dockerfiles start from digest-pinned Docker Official Images for Node 22 on
Debian Bookworm Slim and Python 3.11 on Debian Bookworm Slim. The base images and
the Debian packages installed by `apt` contain many separately licensed works.
Notable direct packages include Chromium, Firefox ESR, Xvfb/X11 components,
Openbox, x11vnc, noVNC, websockify, nftables, iproute2, Git, ripgrep, curl, Tini,
fonts, and CA certificates. Their licenses include permissive, MPL, GPL/LGPL, and
other terms; consult the copyright files under `/usr/share/doc/*/copyright` in the
built images and the corresponding Debian source packages.

Firefox ESR remains installed and a private Firefox profile is created even though
the launcher currently starts Chromium. Firefox is therefore part of the image and
its MPL-2.0 obligations remain relevant.

The base image digests are pinned, but `apt-get` package versions and npm
transitive dependencies are resolved at build time. Before distributing a built
image, create an SBOM for that exact image, export its Debian copyright files and
npm license metadata, review copyleft/source-offer obligations, retain upstream
notices, and scan for vulnerabilities. Re-run that process after every rebuild.

## Artifact handling

The repository intentionally excludes third-party VSIX and tar.gz files. Run
`./codeairlock fetch` on an ARM64 build host. The script accepts only HTTPS GitHub
URLs from the committed lock, downloads to an ignored `.partial` file, verifies the
committed SHA-256 value, and only then moves the file into its build name.

Do not replace a checksum merely because a fresh download differs. Verify the
upstream release, release signature or provenance when available, and review the
license/notices inside the new artifact before changing the lock. The current
manifest has no x86-64 checksums, so the script refuses that architecture.

When redistributing a container, preserve the Kilo and code-server license files
and code-server third-party notices shipped in their artifacts. Apache-2.0 works
may require their license and applicable NOTICE content. The repository's
`THIRD_PARTY_NOTICES.md` is supplementary and does not substitute for notices
bundled by an upstream project.
