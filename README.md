# CodeAirlock

**Isolated code analysis with self-hosted AI.**

CodeAirlock is a small Docker-based harness for using a pinned Kilo Code build on a
source repository with configurable write access while sharply limiting network paths. It combines:

- Kilo Code 7.5.16 and code-server 4.136.2;
- local Python and TypeScript language servers;
- LanceDB 0.26.2 for local semantic indexing;
- a Chromium pixel UI delivered through x11vnc, noVNC, and websockify;
- an allowlisted inference gateway; and
- separate nftables network namespaces with default-deny policies.

The model and embedding services are supplied by the operator. They must expose
OpenAI-compatible `/v1` APIs. The normal configuration accepts private RFC1918
addresses. A trusted, self-hosted public model endpoint can be allowed explicitly
over HTTPS port 443 by repeating its exact pinned IP in the matching
`*_TRUSTED_PUBLIC_IP` setting.

This is an independent community project. It is not affiliated with or endorsed
by Kilo or OpenAI.

> This is a vibe-coded project I built to solve my own problem. I’m uneasy about how much code and context we routinely hand over to AI companies, and I think it’s sometimes worth putting technical limits on what can leave our machines. There may already be a ready-made solution for this; I didn’t spend that long looking. I’m sharing what I put together in case it’s useful to someone else. Thanks for checking it out!

## Security model

CodeAirlock is defense in depth, not a certified zero-exfiltration system. Its goal
is to make the permitted data paths small, explicit, and testable.

```text
host browser
    │  localhost:6080 (pixels, keyboard, pointer)
    ▼
noVNC/websockify namespace ──► workstation VNC
                                  │
                                  ├─ /workspace repository (read-only by default)
                                  ├─ Kilo + code-server + LSP + LanceDB
                                  ├─ Chromium (no inner Chromium sandbox)
                                  └─ only allowed TCP destination
                                             │
                                             ▼
                                      fixed-route gateway
                                       ├─ pinned LLM IP
                                       ├─ pinned embedding IP
                                       └─ optional internal SearxNG IP
```

Every service has its own container, or shares a network namespace with a small
nftables guard container. The policies block DNS, IPv6, Docker-host access,
cloud-metadata addresses, and arbitrary public egress. The gateway accepts only
the exact chat-completions and embeddings routes, rejects caller-selected models
and remote media URLs, strips caller headers, refuses redirects, and does not log
prompts or response bodies.

Projects use read-only access by default. Choosing **Read & write** in the project manager permits
writes to the host repository and sets Kilo edits to require approval. An approved
shell command can also write in that mode; edit approval is not a filesystem
security boundary. Network restrictions are identical in both modes. Kilo may read it and send
relevant text to the configured model. Semantic indexing sends repository chunks
to the configured embedding service. Those two servers are therefore inside the
trusted computing base; network isolation cannot hide data from an endpoint that
is intentionally allowed to receive it.

The host, Docker daemon, host browser, and container images are also trusted. The
host browser can see screen pixels and supplies keyboard and pointer input; noVNC
is a remote-display channel, not a data diode. The gateway holds model credentials
but has no repository mount. The UI container has no repository mount. The
optional SearxNG service receives only a query that the operator approves, but it
and its configured search engines can observe that query.

code-server itself runs without application-layer authentication, but listens only
on workstation loopback. The exposed noVNC port is bound to host loopback and uses
a generated VNC password. These controls assume untrusted clients cannot already
reach the trusted host session.

Chromium is launched with `--no-sandbox` because its nested sandbox does not work
with the container's dropped capabilities and `no-new-privileges` setting. The
outer workstation container and its network namespace are the browser isolation
boundary. Do not treat the container as protection from a hostile Docker daemon,
kernel, base image, or model server.

## Requirements and current platform scope

- Python 3.11 or newer on the host. The launcher and asset verifier run locally,
  and the latter uses `hashlib.file_digest`.
- Docker with Compose v2, nftables support in Linux containers, and enough memory
  for the workstation and model workload.
- An ARM64 host/runtime. The committed artifact lock contains verified checksums
  only for Linux ARM64 Kilo and code-server assets. The fetch script stops on any
  other architecture rather than trusting an unreviewed first download.
- A trusted OpenAI-compatible chat model and embedding model for connected use.

The current release was developed around ARM64 Docker Desktop behavior. A clean
Linux installation and x86-64 images have not been tested. Debian packages and npm
transitive dependencies are not fully locked, so rebuilding later can produce a
different image even though the base-image digests and principal application
versions are pinned. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

The generated network uses the fixed subnet `172.30.88.0/24`; this release supports
one running instance per Docker daemon. Stop any existing CodeAirlock stack before
starting another copy, and check for overlapping Docker or VPN networks. The UI
defaults to localhost port `6080`; set `UI_PORT` if that port is already occupied.

## Quick start with offline synthetic services

The synthetic demo exercises the container topology without contacting a real
model or embedding server. Downloading build artifacts and building images still
requires internet access.

```sh
git clone https://github.com/FrolovKirill/codeairlock.git
cd codeairlock
./codeairlock init
./codeairlock fetch
./codeairlock build
./codeairlock demo
./codeairlock check
./codeairlock verify-demo
```

Open the localhost URL printed by `demo`, then read the generated password from
`runtime/UI_PASSWORD.txt`. The password file stays local and is ignored by Git. Stop the demo
with:

```sh
./codeairlock down
```

`fetch` downloads the exact release URLs in `vendor/assets.lock.json`, verifies
their committed SHA-256 values before use, and leaves the archives ignored under
`vendor/`. Do not commit the downloaded VSIX or tarball.

## Connect trusted model services

Run `./codeairlock init`, then edit `.env` locally. Do not commit it or paste it into
an issue, chat, or build log.

For the default and preferred case, set private addresses:

```dotenv
LLM_BASE_URL=http://llm.internal:11434/v1
LLM_IP=10.0.0.10
LLM_MODEL=your-chat-model
LLM_API_KEY=

EMBED_BASE_URL=http://embeddings.internal:8080/v1
EMBED_IP=10.0.0.11
EMBED_MODEL=your-embedding-model
EMBED_API_KEY=
EMBED_DIMENSION=
```

The hostname is preserved for the HTTP `Host` header and TLS SNI, while the socket
connects to the pinned IPv4 address without runtime DNS.

To use an operator-controlled public endpoint, HTTPS on port 443 is mandatory and
the exact IP must be repeated. This exception is available only for model and
embedding endpoints:

```dotenv
LLM_BASE_URL=https://models.example.org/v1
LLM_IP=YOUR_SERVER_PUBLIC_IPV4
LLM_TRUSTED_PUBLIC_IP=YOUR_SERVER_PUBLIC_IPV4
```

Replace the example hostname and both IP placeholders with those of a server you
operate and trust. Certificate verification remains enabled. Set `CA_BUNDLE` to
an absolute PEM bundle path for a private CA. IP changes require regenerating the
deployment with the new exact value.

## Projects, sessions and the local UI

After configuring model services, run:

```sh
./codeairlock projects
```

A local project manager opens in your browser at `127.0.0.1:6090`. Choose **Add
folder**, enter a name and the absolute path on the Docker host, then select
**Read only** or **Read & write**. On macOS, **Browse…** opens the native folder
picker; other hosts accept a typed path. The path must not contain this deployment
and its secrets. Registration and opening inspect paths and file types without reading source contents.
System/runtime/credential directories and trees containing sockets, FIFOs or devices
are rejected; file types are checked again just before mounting.

Click **Open project**. The manager stops the current environment, mounts the
selected folder at `/workspace`, checks embeddings, completes indexing, and then
shows the isolated desktop. Open or continue sessions in Kilo's existing UI.
Only one project environment runs at a time. Switching interrupts in-flight work;
completed messages, editor settings and indexes remain in that project's Docker
home volume. **Read & write** changes the original host files immediately; there
is no copy-out or merge step. Agent edits and shell commands require approval.

Each registered project has a stable ID and a separate `home-project-<id>` volume.
Other projects' folders, session histories and indexes are not mounted into its
workstation. Paths are immutable after registration: register a new location as a
new project rather than silently attaching old sessions to a different folder.
The registry is host-only `runtime/projects.json`; it and the Docker volumes must
be retained together to keep the project-to-history mapping. Project removal and
volume deletion are intentionally not exposed in this UI.

**Migration:** the first manager/connected launch imports a valid legacy
`REPO_PATH` and `REPO_ACCESS` into one project, preserving `home-private`, the
existing sessions/indexes, and its desktop password. Old mixed history, if any,
stays with that migrated project; it cannot be retrospectively split reliably.
Subsequent edits to these legacy `.env` fields do not change registered projects.
New installations have no project until one is added. `demo` remains a separate
synthetic fixture using `home-demo`.

The manager binds only to host loopback. All API requests require a random bearer
token, exact Host and same-origin requests when Origin is present. The launch link
uses a URL fragment, which the page removes after keeping the token in tab session
storage. No CORS, repository file server or cloud UI is involved. The authenticated
link is also stored in `runtime/manager-url.txt` with mode 0600. Running `projects`
again opens the existing manager. Its port (`--port`, default 6090) must differ
from `UI_PORT` (default 6080).

A read-only bind does not block Unix socket connections. Keep host control sockets
out of project folders for the entire session: preflight checks cannot protect
against a trusted host adding a socket after launch. The host and its processes
remain part of the trusted computing base.

The manager is a trusted host control process with Docker access; it is outside the
agent's network namespace and cannot be reached by the workstation. The browser
shows only project metadata and the noVNC pixel desktop. The VNC password is
separate and stable per project; use **Show desktop password** if prompted. Desktop
connections are removed while switching, and project-specific VNC passwords prevent
an old tab from silently authenticating to another project's desktop.

**Stop environment** stops containers without deleting data; **Cancel & stop**
cancels a pending open/index operation. The UI and CLI serialize lifecycle
operations under the same host lock. Closing a browser tab does not stop the
manager or environment. Ctrl+C in the manager terminal stops the manager (and
cancels its pending operation), while an already-ready environment keeps running.
After reboot, start Docker, run `./codeairlock projects`, and reopen the project.
Saved sessions return; an interrupted model generation does not resume itself.

For terminal use, `./codeairlock up --index` opens the last successfully selected
project and completes indexing. `--project ID` chooses a registered project;
`./codeairlock down` stops it. `up` without `--index` still supports separate
`index` execution. Global model settings remain in `.env` and apply on the next
project open/restart.

At each `up`, a short fixed synthetic string is sent to your embedding endpoint
through the isolated gateway. A one-shot helper shares the workstation firewall
but has no repository mount. The returned vector length configures Kilo and
LanceDB automatically. No external discovery service or direct host HTTP request
is used. Failed detection stops startup before the editor/agent starts.

Leave `EMBED_DIMENSION` empty or omit it. An optional positive value asserts the
expected length; it does not resize vectors, and a mismatch stops startup.

Each repository has an index profile inside its own persistent home volume. The profile
records the embedding endpoint, pinned IP, model name, optional `EMBED_REVISION`, and detected
dimension. Changing any of these stops startup, including switching between two
models with the same dimension. The manager then offers **Rebuild index & open**.
The equivalent CLI action is:

```sh
./codeairlock up --reindex --index
```

A fresh index directory is selected; old indexes and chat history are preserved.
After pulling this update, rebuild images once with `./codeairlock build`.
The first launch after upgrading from the original release also selects a fresh
index because the legacy index has no verified model identity. Subsequent starts
reuse the compatible index. Changing only an API key does not force a rebuild. A changed pinned IP does: it
may point at a different backend even when the URL and model name stay the same. If weights are replaced behind the same endpoint/model name, change
`EMBED_REVISION` yourself: an embeddings response cannot reliably identify weights.
Repository identity uses the resolved host path; moving the repository selects a
separate index. Replacing its contents at the same path should use `up --reindex`.

`index` starts Kilo's loopback-only indexing service if needed, reports only state
and counts, waits for `Complete`, and sends no chat request. Wait for that success
message before using the UI. This keeps initial chat behavior separate from the
repository-wide embedding pass and makes failures easier to diagnose.

The optional `verify-connected-demo` command is limited to the included synthetic
repository, but it launches separate Kilo runs and is not a benchmark or an exact
reproduction of the index-first interactive workflow. No public SearxNG service or
clean Linux installation is claimed as tested here.

## UI permissions and approved search

Generated Kilo policy allows repository reads, globbing, grep, LSP, and semantic
search. File edits are denied in `read-only` mode and require approval in
`read-write` mode. Kilo's built-in web fetch/search remain denied. Shell commands
and external-directory access require UI approval. A shell approval grants code
execution inside the workstation container, including repository writes when
`read-write` is selected. The persistent Kilo home volume is writable in both modes.

Optional web search uses a separate local MCP tool and an internal SearxNG JSON
`/search` endpoint. Configure both `SEARCH_BASE_URL` and its pinned RFC1918
`SEARCH_IP`. The agent can only propose a query. The operator must inspect and
approve that exact query through the host command:

```sh
./codeairlock search list
./codeairlock search approve REQUEST_ID
./codeairlock search deny REQUEST_ID
```

Approval is one-time. Queries expire after ten minutes, cannot be changed during
approval, and are sent at most once. Search results are reduced to title, URL, and
text snippets; result URLs are not fetched. A failed search requires a new proposal
and a new approval. Search has not been validated against a real SearxNG instance
in this release.

## Retry and overload behavior

Chat and embedding calls share one active upstream slot with a bounded queue. At
most five requests can be active or waiting; waiters time out after 15 seconds.
The gateway permits at most three upstream attempts, with a 120-second window for
starting attempts and scheduling retries. Each attempt sets a socket-operation
timeout to the smaller of 75 seconds and the budget remaining at its start. This
is not an overall response deadline: an ongoing response or stream can exceed
120 seconds and retain the active slot.

It retries connection failures that occur before a POST is sent, and HTTP 429,
502, or 503 responses when the delay fits the remaining budget. Backoff begins at
two seconds with jitter, honors `Retry-After`, and refuses a requested delay over
15 seconds. Redirects, TLS failures, HTTP 409, and other server errors are terminal.

The gateway never replays an ambiguous HTTP 408/504, a transport failure after a
POST may have been sent, or any response after headers or stream bytes reached the
caller. Ambiguous upstream failures before response headers are exposed and
exhausted retries trigger a cooldown and terminal HTTP 424, which avoids
multiplying retries in the client SDK. If a response fails after its headers have
been exposed, the gateway closes the connection without replay, a new status
code, or cooldown. Queue saturation uses HTTP 429.
Use `./codeairlock model-status` to inspect counts and cooldown state without viewing
prompt content.

## Configuration reference

| Setting | Meaning |
| --- | --- |
| `LLM_BASE_URL`, `EMBED_BASE_URL` | OpenAI-compatible `/v1` base URL. Credentials in URLs are rejected. |
| `LLM_IP`, `EMBED_IP` | Exact IPv4 destination used by the firewall and socket connection. RFC1918 by default. |
| `LLM_TRUSTED_PUBLIC_IP`, `EMBED_TRUSTED_PUBLIC_IP` | Exact repeated public IP exception; only valid with HTTPS on port 443. |
| `LLM_MODEL`, `EMBED_MODEL` | Exact upstream model names accepted by the gateway. |
| `LLM_API_KEY`, `EMBED_API_KEY` | Optional credentials. The embedding key inherits the LLM key only when both base URLs are identical. |
| `LLM_CONTEXT`, `LLM_MAX_OUTPUT` | Context and output limits advertised to Kilo. |
| `EMBED_DIMENSION` | Optional expected vector length. Empty/omitted means automatic detection; mismatch stops startup. |
| `EMBED_REVISION` | Optional operator version label; change when weights change behind the same model name. |
| `CA_BUNDLE` | Optional absolute path to a PEM CA bundle copied into private runtime state. |
| `REPO_PATH` | Legacy one-time project import; use the project manager for folders. |
| `REPO_ACCESS` | Legacy one-time access import; project-specific mode is saved in the registry. Also sets demo access if explicitly present. |
| `UI_PORT` | Localhost noVNC port, from 1024 through 65535; default `6080`. |
| `SEARCH_BASE_URL`, `SEARCH_IP` | Optional internal SearxNG JSON endpoint and pinned RFC1918 address. Both are needed to enable search. |

## Operator commands

```text
projects                 open the local project manager (default port 6090)
init                     create .env from the safe example
fetch                    download and verify pinned ARM64 release artifacts
build                    fetch artifacts and build both local images
demo                     start the synthetic, network-isolated demo
up                       open the last selected project with current model settings
up --project ID --index   open and index a registered project as one operation
up --reindex             select a fresh index, preserving old indexes and sessions
down                     stop this public-release Compose project
status                   show service status
check                    run fixed network and configuration probes
audit                    inspect all namespace firewall policies
index                    complete semantic indexing before chat
model-status             show queue/retry/cooldown counters
verify-demo              exercise semantic search and LSP with synthetic APIs
verify-connected-demo    exercise the synthetic repository with trusted endpoints
shell                    open a workstation shell
logs [service]           show the last 80 container log lines
search ...               list, approve, or deny search proposals
```

Logs are intentionally limited by the launcher, but third-party processes may
still emit sensitive material. Treat all local runtime state and logs as private.

## Offline tests

The unit and fault-injection tests use only loopback sockets and synthetic data:

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q codeairlock deploy scripts tests demo-repo
```

They cover route and header filtering, public-endpoint validation, one-time search
approval, redirects, request limits, queueing, retry budgets, cooldown, and the
no-replay rules for ambiguous failures and partial streams. They do not prove
kernel isolation on every Docker implementation or model quality. Container
policy checks require a built, running demo and are invoked by `check` and `audit`.

See [SECURITY.md](SECURITY.md) for reporting and operational guidance,
[CONTRIBUTING.md](CONTRIBUTING.md) for public-copy rules, and
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for licensing and redistribution
notes.

Startup regression coverage also includes automatic vector-length detection,
invalid embedding responses, optional dimension assertions, model/IP/revision
changes, index preservation, access-mode generation, and interrupted startup.
On September 11, 2026, 35 offline tests passed. Docker integration on macOS ARM64
verified actual read-only write rejection and successful read-write operations, compatible
index reuse after restart, startup refusal on a same-dimension model change, and
successful Kilo indexing into a fresh directory after `demo --reindex`. Integration
used only the included synthetic repository and synthetic model API.

The project-manager update was also verified on September 11, 2026: 50 public
regression tests passed in offline Docker and 15 project-manager tests passed on
macOS. Browser integration covered adding/opening projects and connecting to the
embedded desktop. Synthetic Kilo sessions were isolated between projects A and B;
saved user/assistant messages survived switching and a full container stop/recreate.
Chromium stale singleton locks are cleared at startup so its persistent profile
can reopen after a container's hostname/PID changes. No real repository or model
was used for those cross-project tests.
