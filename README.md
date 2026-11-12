# pi-agent-runner

Tiny service that runs the [Pi](https://pi.dev) coding agent one-shot
(`pi -p`) inside a fresh pi-sandbox container; backs an Open WebUI tool.

> **Relationship to an interactive setup:** This is the **automated / OWUI**
> path. Running the Pi TUI manually from a terminal is a separate workflow; if
> you only use Pi through Open WebUI, you only need this repo.

**Version: 1.2.6.** Reported by `GET /health` as `version`.

---

## ⚠️ Security posture — read this before exposing the port

This service is designed for a **trusted, private network**. It is not
multi-tenant and has no authentication. Concretely:

| Property | Consequence |
| --- | --- |
| **`/pi/run` is unauthenticated** | Anyone who can reach port 8092 can run arbitrary `pi` prompts billed to your API keys. `pi` has shell tools, so this is **remote command execution inside a sandbox container, as a feature**. |
| **The Docker socket is mounted** and the process runs in the host's `docker` group | The runner has effective root on the host. Combined with the row above, the practical trust boundary is "anyone on the network is root". |
| **Sandbox containers are unconstrained** | No `mem_limit`, `pids_limit`, `cap_drop`, `security_opt`, `read_only`, or `user`. They run as **root** and join your service network, so a task can reach whatever else is on it. |
| **Provider key is readable inside the sandbox** | `OPENCODE_API_KEY` is passed as an env var, so any task can read it with `env`. Unavoidable — `pi` needs the key — but it means the sandbox is as trusted as the key. |
| **Host port published on loopback only** | `"127.0.0.1:8092:8092"`. Callers inside the Docker network should use the service alias anyway (see below), so a wildcard bind was exposure with no benefit. |

If you need to expose this beyond a trusted network, put an authenticating
reverse proxy in front of it and constrain the sandbox containers
(`mem_limit`, `pids_limit`, `cap_drop: [ALL]`, non-root `user`) first. None of
that exists today.

---

## Where config lives

Two images are built from this repo, and they consume config differently. This
distinction matters because the runner service does **not** read any Pi config.

**`pi-sandbox`** (`pi-sandbox.Dockerfile`) — bakes Pi's provider config in at
build time:

```dockerfile
COPY models.json settings.json /root/.pi/agent/
COPY extensions/provider-fallback.ts /root/.pi/agent/extensions/provider-fallback.ts
COPY provider-fallback.json /root/.pi/agent/extensions/provider-fallback.json
```

So `models.json`, `settings.json`, `provider-fallback.json`, and
`extensions/` are **live build inputs**. Removing them breaks the `pi-sandbox`
build, and that image is what every run executes.

**`pi-agent-runner`** (the FastAPI service, `Dockerfile`) — copies only
`main.py` and `requirements.txt`, and reads only:

| Setting | Purpose |
| --- | --- |
| `PI_IMAGE` | Sandbox image to run (`pi-sandbox`) |
| `LLAMA_NETWORK` | Docker network to attach sandbox containers to |
| `LOG_LEVEL` | Python log level (default `INFO`) |
| `OPENCODE_API_KEY` | Forwarded into the sandbox |
| `COMMANDCODE_API_KEY` | Forwarded into the sandbox (second provider) |
| `GIT_TOKEN` | Read-only token used by the clone path only |
| `PI_REPO_ALLOWLIST` | Comma-separated `owner/name`. **Empty disables cloning** |
| `GITEA_BASE_URL` | Clone base URL, e.g. `https://gitea.example.internal` |
| `GIT_CA_FILE` | Optional CA bundle for the clone; empty = system store |

`docker-compose.yml` passes the key with a `:-` default, so an unset secret
degrades to "the provider is unavailable" rather than breaking startup. If it
is missing entirely the runner logs an error at boot
(`No provider API key in the environment ...`) — every run will then fail to
authenticate. That log line is the fastest way to diagnose a misconfigured
deploy.

`LLAMA_NETWORK` defaults to `pi-sandbox-net`. That network must already exist
(`docker network create pi-sandbox-net`) or be supplied by your own compose
project; `docker-compose.yml` declares it `external: true`.

---

## API

Four routes. All are reachable at the service alias from inside the Docker
network — e.g. `http://pi-agent-runner:8092`.

### `GET /health`

```json
{
  "status": "ok",
  "docker": "reachable",
  "version": "1.2.6",
  "provider": "opencode-go",
  "model": "deepseek-v4.1-flash",
  "allowed_providers": ["opencode-go", "commandcode"],
  "allowed_models": {
    "opencode-go": ["deepseek-v4.1-flash", "glm-5.3-flash"],
    "commandcode": ["deepseek/deepseek-v4.1-flash"]
  },
  "repos_configured": 1,
  "repo_clone_ready": true,
  "repo_clone_problem": null
  "allowed_models": {"opencode-go": ["deepseek-v4.1-flash", "glm-5.3-flash"]}
}
```

Returns `503` with `{"status": "error", "detail": ...}` when the Docker daemon
does not answer.

The probe uses a **separate docker client with a 5 s timeout**
(`HEALTH_TIMEOUT_S`), not the shared ~31-minute one. A wedged daemon therefore
produces a fast `503` rather than a request that hangs; anything monitoring
this would otherwise never observe a failure.

The allowlist is echoed deliberately: those constants live in the runner image
while `models.json` is baked into `pi-sandbox`, so a partial deploy can drift
the two. One call shows it.

### `POST /pi/run`

The only way to run a task. One-shot if `session_id` is omitted, session mode
if it is present.

```json
{
  "task": "add a healthcheck to the Dockerfile",
  "timeout": 300,
  "session_id": "my-session"
}
```

| Field | Required | Default | Notes |
| --- | --- | --- | --- |
| `task` | yes | — | 1 – 128 KiB. Becomes **one argv element**; the cap keeps it under the kernel's `ARG_MAX` (exceeding that raises `E2BIG`, which would surface as an opaque 500). |
| `timeout` | no | `300` | 1 – 1800. The task budget, enforced inside the sandbox (session) or by `wait()` (one-shot). |
| `session_id` | no | — | Must start and end alphanumeric; `[A-Za-z0-9_.-]`, max 64. Becomes part of the container name **and** a path component in Pi's session store. Omit for a fresh container per call. |
| `provider` | no | settings.json | Must be supplied **together with** `model`, and must be on the allowlist. |
| `model` | no | settings.json | Must be supplied **together with** `provider`, and must be on the allowlist. |
| `repo` | no | - | Repository slug, `owner/name`. **Session mode only** - see *Repository cloning*. |

`provider` and `model` are all-or-nothing; each alone returns `422`:

- a bare `model` is provider-specific, so it cannot be resolved;
- a bare `provider` would run the default model against a provider that may
  not serve it.

Anything outside the allowlist is rejected with the permitted set named:

```
Provider 'openrouter' is not allowed. Allowed: opencode-go.
Model 'gpt-5' is not allowed on 'opencode-go'.
Allowed: deepseek-v4.1-flash, glm-5.3-flash.
```

Response on success:

```json
{
  "exit_code": 0,
  "stdout": "...",
  "stderr": "",
  "duration_s": 12.4,
  "timed_out": false
}
```

- **`stdout`** is combined stdout+stderr, tailed to the last 2000 lines and
  capped at 256 KiB. Earlier output is dropped and **unrecoverable** — the
  container is removed immediately after the logs are read. A run that needs
  more must write to a session's `/workspace` instead.
- **`stderr`** is **always empty**. docker-py 7.1.0 cannot demux the combined
  log stream, so stderr is folded into `stdout`. The field exists for client
  compatibility; do not branch on it.
- **`timed_out`** is derived from the branch taken, never from wall-clock
  elapsed time. Session mode: `exit_code in (124, 137)` (coreutils TERM / KILL).
  One-shot: `true` only when the container was killed at its deadline.

### Repository cloning

`repo` clones a repository into the session's `/workspace` **before** `pi`
starts, so the agent finds the code already there on its first tool call. It is
**session mode only**: a clone needs a container that outlives it, and one-shot
mode creates a container with a fixed command and removes it immediately, so
`repo` without `session_id` is a `422`.

The clone runs as an `exec_run` in the session's existing container, reusing the
busy flag the caller already holds. That is deliberate: **no new lock and no new
removal path**, so the tombstone invariant described under *Reaping and cleanup*
is untouched.

Idempotent — a second run on the same session **fetches** rather than
re-cloning, so a follow-up task sees upstream changes without losing local work.

**Two gates, in order:**

1. **Shape.** `_REPO_RE` requires `owner/name` with each segment starting
alphanumeric, so `..` cannot match. The value becomes both a path component
   (`/workspace/<name>`) and part of a clone URL, so this is the traversal
   defence.
2. **Allowlist.** `PI_REPO_ALLOWLIST` (comma-separated `owner/name`). **Empty
   disables cloning entirely** — fail-closed. An unlisted slug is rejected and
   the error does **not** reveal the allowlist, since the detail string is shown
   to an LLM.

`PI_REPO_ALLOWLIST` is a deployment setting, not a request field. It is supplied
as a Gitea **variable** rather than a secret: it is configuration, not a
credential. Repo names grant nothing without the token, secrets are write-only so
a typo becomes an invisible 422, and a variable may be echoed in a log while
debugging.

**Two settings are required for cloning to work at all:**

| Env var (what this service reads) | Gitea variable name |
| --- | --- |
| `PI_REPO_ALLOWLIST` | `PI_REPO_ALLOWLIST` |
| `GITEA_BASE_URL` | **`URL_GITEA_BASE_URL`** |

The base URL differs from the env var name because the Gitea variable namespace
forbids a leading `GITEA_`. Only the stored name differs; `docker compose` and
`main.py` both read `GITEA_BASE_URL`. `GET /health` reports `repo_clone_ready`,
which is false when the allowlist is empty **or** the base URL is unset, so a
half-configured deployment is visible from one call instead of failing at clone
time.

**Credential handling.** `GIT_TOKEN` is passed per-exec rather than read from the
container's environment, so a container *adopted* from a previous process still
works. The clone uses a credential helper that reads `$GIT_TOKEN` at call time,
so the token never reaches `.git/config`, `git remote -v`, or any diff. Scope the
token read-only unless the agent needs to push.

**TLS.** The internal CA is installed into the image trust store, so `git`
verifies `gitea.example.internal` normally. `GIT_SSL_NO_VERIFY` is **not** used — that
would disable verification for every host, the provider endpoints included.
`tests/test_internal_ca.py` guards this.

`/health` reports `repos_configured` as a **count**, never the names.

### `GET /pi/sessions`

```json
{
  "sessions": [
    {"session_id": "my-session", "idle_seconds": 12.0, "age_seconds": 3400.2,
     "busy": false, "ready": true}
  ],
  "limit": 5
}
```

`ready` is `false` while a session's container is still being created.

### `POST /pi/session/close/{session_id}`

Stops and removes the session's container. `{"status": "closed", ...}` on
success. `404` if unknown, `409` if the session is busy or already closing.

### Status codes

| Status | Route | Meaning | Retryable |
| --- | --- | --- | --- |
| `200` | `/pi/run` | Completed. Check `exit_code` and `timed_out`. | — |
| `409` | `/pi/run` | Session busy, or being closed. | yes, shortly |
| `422` | `/pi/run` | `provider`/`model` mismatch, not on the allowlist, or a field failed validation | no |
| `429` | `/pi/run` | `MAX_CONCURRENT_SESSIONS` (5, session mode) or `MAX_CONCURRENT_RUNS` (8, one-shot) reached | yes, after freeing one |
| `500` | `/pi/run` | The run failed, **or** the wait was lost (see below) | maybe |
| `503` | `/health` | Docker socket unreachable | yes |

Every `detail` string is written to be shown to a caller. **Do not collapse
non-2xx into "could not reach the service"** — a `429` means the service was
reached and answered deliberately.

### The `500` on a lost wait

A transport blip between the runner and the daemon can end a one-shot's
`wait()` while the container is still healthy. In that case a `500` means:

- the task **did not fail** — it is probably still running;
- the output is **not recoverable** (nothing is reading it);
- the container is left in place and removed by the reaper at its deadline (at
  most `timeout + 30 s` later), so it cannot burn tokens indefinitely.

`detail` says so explicitly. This is a deliberately different contract from
"your task ran and failed", which is a `200` with a non-zero `exit_code`.

---

## Providers and models

Two providers are permitted, enforced in `main.py` (`_ALLOWED_PROVIDERS` /
`_ALLOWED_MODELS`):

| Provider | Model | Notes |
| --- | --- | --- |
| `opencode-go` | `deepseek-v4.1-flash` | default |
| `opencode-go` | `glm-5.3-flash` | requestable; separate allowance |
| `commandcode` | `deepseek/deepseek-v4.1-flash` | custom provider |

Because `provider`/`model` can arrive from an LLM-driven tool call, the
enforcement is server-side: config alone would still let a caller name any
provider `pi` happens to know about, and all of them bill to a key this service
holds.

### The two DeepSeek entries are the same model

`opencode-go` and `commandcode` both serve **DeepSeek V4.1 Flash**, so choosing
between them cannot change output quality. The only real difference is which
subscription is billed and which rate-limit pool is used. That makes it a
**scheduling** decision, not a routing one, so there is deliberately no routing
logic. The split is expressed in `settings.json` instead:

| Role | Provider | Why |
| --- | --- | --- |
| `programmer` | `commandcode` | writer, largest consumer -> second pool |
| `reviewer`, `verifier` | `opencode-go` | readers, off the writer's pool |
| `oracle` | `opencode-go` / glm | explicitly invoked only |

Those pins resolve server-side, so the OWUI model cannot accidentally spend the
wrong bucket.

### `models.json` is an override, not a declaration

For a provider `pi` already ships, an entry **replaces** its built-in metadata
and anything omitted is lost. `opencode-go` is built-in and therefore has no
entry here.

`commandcode` genuinely needs one, because it is not a pi built-in:

```json
{
  "providers": {
    "commandcode": {
      "baseUrl": "https://api.commandcode.ai/provider/v1",
      "api": "openai-completions",
      "apiKey": "$COMMANDCODE_API_KEY",
      "models": [
        { "id": "deepseek/deepseek-v4.1-flash",
          "contextWindow": 1000000, "maxTokens": 32768,
          "cost": { "input": 0.15, "output": 0.60,
                    "cacheRead": 0.003, "cacheWrite": 0.15 } }
      ]
    }
  }
}
```

**A malformed entry silently removes the whole provider.** pi validates
`models.json` entry by entry and drops the provider on any schema error, with
only a warning line:

```
Invalid models.json schema:
  - providers.commandcode.models.0.cost.cacheRead:
      must have required properties cacheRead, cacheWrite
```

Every model reference then fails as `Unknown subagent model ...`, which reads
like an auth or naming problem and is neither. Two traps:

- `cost` is optional, but **if present it needs all four** of `input`, `output`,
  `cacheRead`, `cacheWrite`. A two-field block is invalid.
- `modelOverrides` accepts a *partial* cost block while a provider's `models[]`
  does not. The same shape is legal in one place and fatal in the other.

Check with `pi --list-models` — a correct entry appears in the table, a broken
one does not.

### No automatic fallback

`provider-fallback.json` ships **disabled**:

```json
{ "enabled": false, "providers": {} }
```

Substituting a model silently returns a verdict from a model the caller did not
choose, and `glm-5.3-flash` draws on a separate allowance that must only be
spent when explicitly requested. An automatic deepseek -> glm chain would burn
it on ordinary rate-limiting.

With the same model on two providers, the useful failover is **same-model
across providers** — quality-neutral, and it belongs in the provider choice
rather than a substitution chain. That is not implemented; what is implemented
is per-role pinning, which spreads load without any implicit swap.

---

## Sessions and conversation continuity

There are **two separate kinds** of continuity, and conflating them is a real
bug that this service had:

1. **Container continuity** — `/workspace` files survive because the container
   is reused.
2. **Conversation continuity** — Pi's own message history, which requires
   passing `--session-id` to `pi`.

`pi -p` is one-shot: without `--session-id` it starts a fresh conversation on
every `exec_run` in the same container. Files persist; the agent's memory does
not. `_build_pi_cmd()` forwards the flag in session mode only.

What that means in practice:

- Omit `session_id` → a fresh container per call. Nothing carries over.
- Reuse the same `session_id` → same container, same `/workspace`, **same
  conversation history**. Follow-ups like "now add a test for that" work.
- Runs on one `session_id` are serialized; a concurrent second run gets `409`.
- `MAX_CONCURRENT_SESSIONS` is 5; a 6th returns `429`.
- Sessions are reaped after 30 min idle or 24 h from creation. The age survives
  runner restarts — it is read from the container's own creation timestamp.

**All limits are compile-time constants in `main.py`.** Changing
`MAX_CONCURRENT_SESSIONS`, `SESSION_IDLE_TIMEOUT`, `SESSION_MAX_AGE`,
`REAP_INTERVAL_S`, or the timeout bounds requires a code change **and a
redeploy**; none is a request field or an environment variable.

### Why a chat id is a bad `session_id`

Keying `session_id` off a chat id looks natural and has two problems:

1. **The 24 h age cap kills live conversations.** `SESSION_MAX_AGE` counts from
   container creation, not last use. A chat older than 24 h loses its workspace
   mid-conversation.
2. **Five chats exhaust the cap.** `MAX_CONCURRENT_SESSIONS = 5` is a hard 429
   on the 6th, with no LRU eviction. Closing is manual or waits for the 30 min
   idle reaper.

A memory manager needs LRU eviction and a decision about whether the age cap
should be idle-based. Neither exists today.

---

## OWUI tool configuration

The client is a Python function in Open WebUI. Its `runner_host` valve **must**
point at the same-network Docker DNS alias:

```
http://pi-agent-runner:8092
```

**Do NOT use the host published port.** Calling a host-published port from
inside the same Docker network creates a hairpin (container → host published
port → back into the same network) that Docker's userland proxy blackholes,
causing intermittent `ConnectTimeoutError`.

The DNS alias is stable across redeployments (unlike the container IP, which
changes on restart) and is the correct same-network route. The host port is
bound to `127.0.0.1` and exists only for host-side debugging.

### Client compatibility

The tool lives in the tools repo (`pi-agent-openwebui-tool.py`), not here. It
mirrors this service's allowlist and patterns so a stale value fails locally with
a precise message instead of a round-trip `422` — **update both in the same
commit**, or the tool rejects a request this service would have accepted.

Client versions and what they send differently:

| Tool version | Adds |
| --- | --- |
| 1.3.0 | documents delegation; default timeout `600` (a delegated run is several agent loops) |
| 1.4.0 | `commandcode` in the provider allowlist |
| 1.5.0 | **`repo`** — before this, no client ever sent it |

That last row matters when debugging: this service has accepted `repo` since
v1.2.0, but a client older than 1.5.0 silently omits the field, leaving
`/workspace` empty. An empty workspace is therefore **not** evidence the clone
path is broken — check what the client sent first.

A commit to that repo has no effect until the file is pasted into Open WebUI; it
is source-of-truth only, with no deploy workflow.

---

## Timeouts

Three independent deadlines, and all three matter:

| Deadline | Where | Effect |
| --- | --- | --- |
| `timeout` (request field, 1–1800 s) | In-container coreutils `timeout` (session mode) / `wait()` (one-shot) | The actual task budget; surfaces as `timed_out: true` with exit 124/137 |
| `DOCKER_TIMEOUT_S` (~1890 s) | docker-py client, built in `get_client()` | Caps the HTTP call itself. Without it docker-py's 60 s default applies per request and would abort a session run that is silent while the model thinks. |
| `timeout + 10` | the Open WebUI tool's `requests.post` | How long the client waits before reporting `status: "timeout"` |

A session run that exceeds its `timeout` is killed **inside** the container, so
the container stays healthy and usable for the next run. A one-shot run that
exceeds `timeout + WAIT_GRACE_S` (30 s) is killed and removed by the runner.

Exit codes differ by mode. Session runs surface `124` (TERM) or `137` (KILL);
one-shot timeouts surface `exit_code: -1` with `timed_out: true`, because the
container is killed rather than allowed to exit.

---

## Reaping and cleanup

| Mechanism | Runs | Removes |
| --- | --- | --- |
| Idle/age reaper | every `REAP_INTERVAL_S` (60 s) | sessions idle > 30 min or older than 24 h |
| Orphan sweep | every 60 s | `pi-session-*` containers with no in-memory entry (failed close, previous process) |
| Stray one-shot sweep | every 60 s | `pi-one-shot-*` containers past their `pi-deadline-epoch` label |
| Startup reconciliation | once, at boot | every `pi-one-shot-*`; **adopts** surviving `pi-session-*` |

The runner labels every container it creates (`pi-agent-runner=one-shot|session`,
plus `pi-session-id` / `pi-deadline-epoch`), so a restarted process can find
them. The session registry is in-process state; the containers are not.

**The invariant that keeps being got wrong:** a container must never be
**adopted** while it is being **removed**. Adoption happens when
`containers.create()` returns 409 — the name is still taken — so any path that
removes a `pi-session-<sid>` container must tombstone `sid` in `_closing` **in
the same lock critical section** that identifies the victim. Tombstoning in a
second acquisition only narrows the race. All three removal paths
(`_reap_idle_sessions`, `close_session`, `_sweep_orphan_sessions`) do this.
If you add a fourth removal path, copy the pattern.

---

## Deployment

`docker-compose.yml` expects an existing external network and a mounted Docker
socket:

```yaml
networks:
  pi-sandbox-net:
    external: true
volumes:
  - /var/run/docker.sock:/var/run/docker.sock
```

The service runs as a non-root user in the host's `docker` group so the socket
is readable. That GID is host-specific, so `Dockerfile` takes it as a build
arg, defaulting to 984:

```bash
docker build --build-arg DOCKER_GID=$(stat -c '%g' /var/run/docker.sock) .
```

The image carries a `HEALTHCHECK` using `python -c` (the slim image has no
`curl`). `restart: unless-stopped` only recovers a *dead* process, never a
live-but-broken one — which is what a wedged Docker socket produces — so the
healthcheck is what actually catches that.

`GET /health` returns the running version, so `docker inspect` health plus that
field confirms what was actually deployed.

### Building `pi-sandbox`

`pi-sandbox` is a separate image built from `pi-sandbox.Dockerfile`. It must be
rebuilt whenever `models.json`, `settings.json`, `provider-fallback.json` or
`extensions/` change, because those are baked in at build time.

---

## Known gaps

- **Tests cover only the static guards.** `tests/run_static_guards.py` checks
  source invariants (CA trust, no verification bypass, publication hygiene,
  version consistency) and needs only the stdlib. `_created_at_monotonic()`,
  `_build_pi_cmd()`, `_validate_provider_model()` and the tombstone invariant
  are still **untested** - all pure and testable with a fake container.
- **No LRU eviction for sessions** (see the session section).
- **No sandbox resource limits** (see Security posture).
- **No authentication** on any route.
- **`main.py` is a single ~62 KB module** holding config, orchestration, the
  session registry, the reaper, and the routes. The concurrency bugs arrived in
  pairs because the adoption/removal invariant is enforced by hand in several
  places; a `SessionRegistry` owning `reserve`/`release`/`begin_close`/
  `end_close` would make it structural.

---

## License

MIT
