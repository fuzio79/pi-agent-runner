"""pi-agent-runner — FastAPI service that runs the Pi coding agent one-shot.

Architecture (v1.1.8):
  OWUI tool ──HTTP──> pi-agent-runner (this service, :8092)
                          │
                          │ docker-py (Docker socket)
                          │
                          └─> pi-sandbox container (fresh per run)
                                  │
                                  └─> pi -p <task>  → stdout → back to OWUI

Config (models.json, settings.json, provider-fallback.json, extensions/) is
baked into the pi-sandbox image — no runtime config bind mounts needed.

Provider/model selection — ALLOWLIST, not a preference:
  Exactly one provider is permitted, `opencode-go` (the OpenCode Go
  subscription, and a built-in provider in the pi CLI). Exactly two models are
  permitted on it:

      deepseek-v4.1-flash   default (settings.json)
      glm-5.3-flash         requestable fallback

  `_ALLOWED_PROVIDERS` / `_ALLOWED_MODELS` below are the enforcement point and
  the authoritative answer to "what may run". models.json narrows what pi can
  see; this file decides what a caller may ask for. Both are needed: config
  alone would let a caller name any provider pi happens to know.

  Callers may override per run by passing `provider` AND `model`. Both are
  required together: a bare `model` is provider-specific, and a bare
  `provider` would run the default model against a provider that may not
  serve it.

  All options are emitted BEFORE a `--` separator and the task text AFTER it.
  `pi` documents `pi [options] [--] [@files...] [messages...]`, so `--` ends
  option parsing: verified that `pi -p -- -leading-dash` treats the token as
  message text while `pi -p -leading-dash` fails with "Unknown option".
  The task is therefore never reinterpreted as a flag, and it is passed through
  byte-for-byte rather than being rewritten to dodge the parser.

Session mode:
  A `session_id` creates a persistent container (pi-session-<id>) reused across
  runs. There are TWO distinct kinds of continuity, and conflating them was a
  real bug:

    1. Container continuity — /workspace files survive between calls because
       the container is reused. This always worked.
    2. Conversation continuity — Pi's own message history. This requires
       passing `--session-id <id>` to `pi`, which the runner NEVER DID until
       v1.1.8. `pi -p` is one-shot: without the flag it starts a fresh
       conversation in the same container every time, while still writing a
       session file that is never read back.

  `_build_pi_cmd()` now forwards `--session-id` in session mode only. Verified
  against the live image: two separate `pi -p` invocations sharing
  `--session-id` do retain context.

  Runs on one session are serialized (409 if busy), and only
  MAX_CONCURRENT_SESSIONS may exist. A background reaper removes a session
  after SESSION_IDLE_TIMEOUT of inactivity or SESSION_MAX_AGE from creation.

Reaping invariants (the part that keeps being got wrong — read before editing):
  A container must never be ADOPTED while it is being REMOVED. Adoption happens
  when `containers.create()` returns 409, i.e. the name is still taken, so any
  path that removes a `pi-session-<sid>` container MUST tombstone `sid` in
  `_closing` — and MUST do it in the SAME `_session_lock` critical section as
  the `_sessions` check/pop that identified the victim. Tombstoning in a second
  lock acquisition leaves a window in which another thread reserves the id and
  adopts the container mid-removal; that is the whole bug class.

  Three paths remove session containers, and each tombstones atomically:

    1. `_reap_idle_sessions`    — pops + tombstones under one lock
    2. `close_session`          — pops + tombstones under one lock
    3. `_sweep_orphan_sessions` — tombstones under the lock when claiming

  #17 and #19 were this defect on paths 1/2; path 3 was missed until v1.1.7.
  If you add a fourth removal path, tombstone in the same critical section that
  claims the victim, then remove outside the lock, then discard the tombstone
  in a `finally`. That is the entire invariant.

  `_sweep_stray_one_shots` is the counterpart for one-shot containers: a
  one-shot left running by a lost wait (case 3 below) carries a deadline label
  and is removed once that deadline passes, so it cannot burn provider tokens
  forever with no consumer for its output.

Docker API notes (docker-py 7.1.0):
  - containers.run() has an unstable return shape across versions, so we
    use explicit create/start/wait/logs/remove instead.
  - container.logs() does not accept a `demux` kwarg in 7.1.0, so we
    request combined stdout+stderr.
  - container.wait(timeout=N) does NOT raise requests.exceptions.ReadTimeout
    when N elapses. Over the unix socket the failure arrives from urllib3
    (`UnixHTTPConnectionPool(...): Read timed out.`) and is only reachable
    through the broad `requests.exceptions.RequestException`. Selecting the
    timeout branch by exception class was therefore dead code.
  - A wait failure says NOTHING about the outcome. Three cases must be told
    apart by asking the daemon, not by the exception:
      1. container still running and the deadline elapsed -> our timeout
      2. container exited during the stall                  -> the run finished
      3. container still running but well before the deadline -> a transport
         blip; the task is fine and must NOT be killed or reported as a result
  - containers.create() does NOT accept run()-only kwargs.
  - docker.from_env() defaults to a 60-second timeout on *every* request,
    shorter than the runs we allow, so the client is built with an explicit
    DOCKER_TIMEOUT_S below. /health deliberately uses a SECOND client with a
    short timeout, because a wedged daemon must fail the probe, not hang it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import docker
import docker.errors
import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# ── Logging ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("pi-agent-runner")

VERSION = "1.2.6"

# ── Configuration ─────────────────────────────────────────────────────────
DEFAULT_TIMEOUT = 300
MAX_TIMEOUT = 1800
SESSION_IDLE_TIMEOUT = 1800   # 30 minutes
SESSION_MAX_AGE = 86400       # 24 hours
MAX_CONCURRENT_SESSIONS = 5
MAX_CONCURRENT_RUNS = 8       # concurrent one-shot runs (each spawns a container)
REAP_INTERVAL_S = 60

# Default model, mirrored from settings.json. Baked into the image and read back
# there by pi; duplicated here only so /health can report it and so a mismatch
# between the two files is visible instead of silent.
DEFAULT_PROVIDER_NAME = "opencode-go"
DEFAULT_MODEL_NAME = "deepseek-v4.1-flash"

# ── Provider / model allowlist ────────────────────────────────────────────
#
# THIS IS THE ENFORCEMENT POINT. models.json decides what pi can see; this
# decides what a caller may ask for. Both are required — config alone would
# still let a caller name any provider pi happens to know about.
#
# Exactly one provider, two models. To add a model, add it here AND to
# models.json, or the two drift and the caller gets a confusing rejection from
# the wrong layer.
_ALLOWED_PROVIDERS = ("opencode-go", "commandcode")

_ALLOWED_MODELS: dict[str, tuple[str, ...]] = {
    "opencode-go": ("deepseek-v4.1-flash", "glm-5.3-flash"),
    # Command Code (GOAT). Model ids carry a vendor slash here, which is why
    # _MODEL_RE admits '/'. Verified from the provider's own model listing:
    # context 1,000,000 tokens, max output 32,768.
    "commandcode": ("deepseek/deepseek-v4.1-flash",),
}

# A task becomes ONE argv element, so it must stay well under the kernel's
# ARG_MAX (~2 MB on Linux) — exceeding it raises OSError/errno 7 (E2BIG), which
# is neither a DockerException nor a RequestException and would escape as a
# bare 500. 128 KiB is ~32k tokens: generous for an agent task, safely bounded.
MAX_TASK_CHARS = 128 * 1024

# Output is posted into OWUI's context window, so cap it hard.
MAX_LOG_CHARS = 256 * 1024
MAX_LOG_LINES = 2000

# Extra seconds of headroom beyond the requested timeout before we give up
# waiting on the container, so a task that finishes right at the deadline is
# not reported as timed out. Only affects when we *kill* a hung run.
WAIT_GRACE_S = 30

# How close to the deadline a failed wait must be before we call it a timeout
# rather than a lost connection. A wait that dies well before the deadline is a
# transport problem, and killing the container there would destroy live work.
DEADLINE_SLACK_S = 5

# Cap for the docker-py client itself. The largest run we permit is
# MAX_TIMEOUT, and the one-shot path waits MAX_TIMEOUT + WAIT_GRACE_S. Without
# this, docker-py's 60s default applies per request and would abort a
# legitimately silent session run. See the module docstring.
DOCKER_TIMEOUT_S = MAX_TIMEOUT + WAIT_GRACE_S + 60

# /health must answer quickly even when the daemon is wedged: a probe that
# hangs is worse than one that fails, because the caller sees neither a result
# nor an error. Kept far below DOCKER_TIMEOUT_S on purpose.
HEALTH_TIMEOUT_S = 5

# Sandbox image / network are deploy-time settings, so read them from the
# environment (docker-compose.yml supplies them) instead of hardcoding.
PI_IMAGE = os.environ.get("PI_IMAGE", "pi-sandbox")
LLAMA_NETWORK = os.environ.get("LLAMA_NETWORK", "pi-sandbox-net")

# Labels let a restarted runner find the containers it owns.
LABEL_KEY = "pi-agent-runner"
LABEL_ONE_SHOT = {LABEL_KEY: "one-shot"}
LABEL_SESSION = {LABEL_KEY: "session"}
# Unix epoch after which a one-shot container may no longer be running. Set at
# creation from the requested timeout, so a container left alive by a lost wait
# is reclaimable by the reaper rather than by the next runner restart.
LABEL_DEADLINE = "pi-deadline-epoch"

# Environment variables that carry API keys into the sandbox container.
# These are read from the runner's own environment (injected by the deploy
# workflow from Gitea secrets) and forwarded to the pi-sandbox container.
#
# Only opencode remains: openrouter and grok were removed with their models.
# The `opencode-go` provider is a built-in provider in the `pi` CLI and reads
# `OPENCODE_API_KEY` — the same env var as the `opencode` provider. There is no
# separate OPENCODE_GO_API_KEY.
_ENV_KEYS = (
    "OPENCODE_API_KEY",
    "COMMANDCODE_API_KEY",
    # Used ONLY to clone an allowlisted repo into a session's /workspace.
    # Read-only scope in the deployment; the clone helper reads it at call
    # time, so it never lands in .git/config or `git remote -v`.
    "GIT_TOKEN",
)

# Provider ids are flat (`opencode-go`). Model ids may carry a vendor slash. The
# regexes are SHAPE checks; `_validate_provider_model` is the allowlist.
#
# The first character must be alphanumeric: a leading `-` would make the value
# itself flag-shaped, and `--provider -x` is a malformed invocation even though
# argv cannot inject a shell command. `--` protects the task; this protects the
# option values.
_PROVIDER_RE = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$"
_MODEL_RE = r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$"

# Session ids become BOTH a container name and a `pi --session-id` value, and
# pi turns that value into a path component under
# ~/.pi/agent/sessions/<cwd>--<id>/.
#
# This pattern is deliberately IDENTICAL to pi's own rule, quoted from the
# error that pi emits:
#   "Session id must be non-empty, contain only alphanumeric characters, '-',
#    '_', and '.', and start and end with an alphanumeric character"
#
# The previous pattern was `[A-Za-z0-9_.-]{1,64}`, a strict SUPERSET: it
# accepted `..`, `.`, `.abc`, `abc.`, `-a` and `a-`, all of which pi rejects.
# With --session-id now forwarded, that superset meant a caller could pass `..`
# and have it used in a filesystem path. Matching pi exactly removes the whole
# class without a separate traversal special-case: `..` cannot match because it
# must start with an alphanumeric character.
_SESSION_ID_RE = r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,62}[A-Za-z0-9])?$"

# Gitea base URL used for clones. NOT hardcoded: the hostname is deployment
# detail, and hardcoding it in the clone URL made the public snapshot publish an
# internal name. Format: scheme + host, no trailing slash.
_GITEA_BASE_URL = os.environ.get("GITEA_BASE_URL", "").rstrip("/")


def _base_url_problem(url: str) -> str | None:
    """Return a human explanation if `url` cannot be a clone base URL.

    WHY THIS EXISTS. The variable was set to `gitea.example.internal` with no scheme.
    git then treated the clone target as a LOCAL FILESYSTEM PATH:

        fatal: repository 'gitea.example.internal/LLM/x.git' does not exist

    That message names neither the variable nor the missing scheme, and it sent
    the investigation into code that was correct the whole time. A
    misconfiguration should name itself.
    """
    if not url:
        return "GITEA_BASE_URL is empty"
    if "://" not in url:
        return (
            f"GITEA_BASE_URL={url!r} has no URL scheme; it needs e.g. "
            f"'https://{url}'. Without one git treats the clone target as a "
            "local path and fails with 'repository does not exist'."
        )
    scheme = url.split("://", 1)[0].lower()
    if scheme not in ("http", "https"):
        return f"GITEA_BASE_URL uses unsupported scheme {scheme!r}"
    return None

# Optional path to a CA bundle the clone should trust. Empty means the system
# trust store, the normal case when the internal CA is baked into the image. A
# deployment whose private CA is NOT baked in can point this at a mounted bundle
# instead of disabling verification.
_GIT_CA_FILE = os.environ.get("GIT_CA_FILE", "")

# Repository slug, owner/name. Each segment starts alphanumeric, so `..` cannot
# match -- the same reasoning as _SESSION_ID_RE, because this value also becomes
# a PATH component (/workspace/<name>) and part of a clone URL. The regex only
# bounds the SHAPE; PI_REPO_ALLOWLIST decides what may actually be cloned.
_REPO_RE = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$"

# Docker reports creation times with nanosecond precision; datetime.fromisoformat
# accepts at most microseconds, so trim the fraction before parsing.
_ISO_FRAC_RE = re.compile(r"^(?P<head>[^.]*)\.(?P<frac>\d+)(?P<tz>.*)$")

# Errors raised by a dead/unresponsive daemon come from both libraries.
_DAEMON_ERRORS = (
    docker.errors.DockerException,
    requests.exceptions.RequestException,
)


_client: docker.DockerClient | None = None
_health_client: docker.DockerClient | None = None
# Guards client construction only. Never held while calling the daemon, so it
# cannot interact with `_session_lock` and cannot deadlock.
_client_lock = threading.Lock()

_sessions: dict[str, "_Session"] = {}
# Ids whose container is currently being stopped/removed. Dropping the
# `_sessions` entry before the (slow, unlocked) removal would otherwise let a
# concurrent request for the same id hit 409 and ADOPT the container that is
# about to disappear — then exec into a dying container.
_closing: set[str] = set()
_session_lock = threading.Lock()
_run_gate = threading.BoundedSemaphore(MAX_CONCURRENT_RUNS)


def get_client() -> docker.DockerClient:
    """Return the shared long-timeout docker client.

    Built under a lock: the routes are sync `def`, so two concurrent first
    requests would otherwise each construct a client and orphan one of them
    along with its urllib3 connection pool.
    """
    global _client
    with _client_lock:
        if _client is None:
            # Explicit timeout: docker-py's 60s default is shorter than the runs
            # we allow, and applies to exec_run's poll as well as its start call.
            _client = docker.from_env(timeout=DOCKER_TIMEOUT_S)
        return _client


def _get_health_client() -> docker.DockerClient:
    """Return a docker client with a short timeout, for /health only.

    Separate from `get_client()` on purpose: reusing the long-timeout client
    would make a probe against a wedged daemon block for ~31 minutes, so the
    healthcheck could never report failure — only hang.
    """
    global _health_client
    with _client_lock:
        if _health_client is None:
            _health_client = docker.from_env(timeout=HEALTH_TIMEOUT_S)
        return _health_client


# ── Request / response models ────────────────────────────────────────────
class RunRequest(BaseModel):
    task: str = Field(..., min_length=1, max_length=MAX_TASK_CHARS)
    timeout: int = Field(DEFAULT_TIMEOUT, ge=1, le=MAX_TIMEOUT)
    session_id: str | None = Field(None, pattern=_SESSION_ID_RE)
    # Optional provider/model override. Omit both to use the default. They are
    # all-or-nothing and BOTH are checked against the allowlist: see
    # `_validate_provider_model`.
    provider: str | None = Field(
        None, min_length=1, max_length=64, pattern=_PROVIDER_RE,
    )
    model: str | None = Field(
        None, min_length=1, max_length=128, pattern=_MODEL_RE,
    )
    # Session mode only: cloning needs a container that outlives the clone.
    repo: str | None = Field(
        None, min_length=3, max_length=165, pattern=_REPO_RE,
    )


class RunResponse(BaseModel):
    exit_code: int
    stdout: str
    # Always empty: docker-py 7.1.0 cannot demux the combined log stream, so
    # stderr is folded into stdout. Kept for client compatibility — do not rely
    # on it carrying anything.
    stderr: str
    duration_s: float
    timed_out: bool = False


# ── Helpers ───────────────────────────────────────────────────────────────
def _env_for_sandbox() -> dict[str, str]:
    """Collect API-key env vars to forward into the sandbox container."""
    env: dict[str, str] = {}
    for key in _ENV_KEYS:
        val = os.environ.get(key)
        if val:
            env[key] = val
    return env


def _validate_provider_model(
    provider: str | None, model: str | None,
) -> tuple[str | None, str | None]:
    """Return `(provider, model)` after checking them against the allowlist.

    Raises HTTPException(422) for anything not explicitly permitted.

    Why reject rather than warn: this is a spending control. pi knows about
    ~100 models across three providers, and every one of them bills to a key
    this service holds. `provider`/`model` come from an LLM-driven tool call, so
    "unknown provider" must be a hard stop, not a fallback to the default.
    """
    if provider is None and model is None:
        return None, None

    # Both or neither. Each direction breaks differently:
    #   - a bare `model` is provider-specific, so it cannot be resolved;
    #   - a bare `provider` would run settings.json's default model against
    #     that provider, which may not serve it.
    if provider is not None and model is None:
        raise HTTPException(
            status_code=422,
            detail="`provider` requires `model`; the default model is provider-specific.",
        )
    if model is not None and provider is None:
        raise HTTPException(
            status_code=422,
            detail="`model` requires `provider` (a model name is provider-specific).",
        )

    assert provider is not None and model is not None

    if provider not in _ALLOWED_PROVIDERS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Provider {provider!r} is not allowed. "
                f"Allowed: {', '.join(_ALLOWED_PROVIDERS)}."
            ),
        )

    permitted = _ALLOWED_MODELS.get(provider, ())
    if model not in permitted:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Model {model!r} is not allowed on {provider!r}. "
                f"Allowed: {', '.join(permitted)}."
            ),
        )

    return provider, model


def _build_pi_cmd(
    task: str,
    provider: str | None,
    model: str | None,
    session_id: str | None = None,
) -> list[str]:
    """Build the `pi` CLI argv: options first, then `--`, then the task.

    `pi` documents `pi [options] [--] [@files...] [messages...]`, and `--` ends
    option parsing. Options must therefore be emitted BEFORE it and the task
    AFTER it — the reverse order would leave the task in option position, which
    is exactly the flag-injection bug this replaces.

    Previously the task was placed directly after `-p`, so a task starting with
    `-` was parsed as an option: `pi -p -leading-dash` fails with
    "Unknown option: -leading-dash", while `pi -p -- -leading-dash` treats it
    as message text. Rewriting the task with a leading space would also have
    worked, but that sends the agent a task that differs from what was asked.

    `session_id` is what makes session mode carry CONVERSATION, not just files.
    `pi -p` is one-shot: without `--session-id` it starts a fresh conversation
    on every exec_run in the same container, so /workspace persisted while Pi's
    memory did not. Verified that two separate `pi -p` calls sharing
    `--session-id` do retain context.

    The command stays an argv list (exec form, no shell), so no value can inject
    a shell command regardless.
    """
    cmd = ["pi", "-p"]
    if provider:
        cmd += ["--provider", provider]
    if model:
        cmd += ["--model", model]
    if session_id:
        # Options must all precede the `--` terminator.
        cmd += ["--session-id", session_id]
    # Terminator after all options: everything past this point is data.
    cmd += ["--", task]
    return cmd


def _redact(text: str) -> str:
    """Strip credential-shaped fragments from git output.

    Git echoes URLs and a credential helper failure can name the password it
    tried. This text reaches both the service log and an LLM-driven client, so
    both are masked.

    Deliberately NARROW: redacts scheme://user:pass@host userinfo and any
    password=... token, nothing else. Over-redacting would hide the diagnostic
    this exists to carry.
    """
    text = re.sub(
        r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^/@\s]+@", r"\1<redacted>@", text
    )
    text = re.sub(r"(?i)(password=)\S+", r"\1<redacted>", text)
    return text


def _truncate(text: str) -> str:
    """Cap captured output so a chatty run cannot blow up memory/OWUI."""
    if len(text) > MAX_LOG_CHARS:
        return "…[earlier output truncated]…\n" + text[-MAX_LOG_CHARS:]
    return text


def _snapshot_logs(container: Any) -> str:
    """Fetch a bounded tail of combined output. Never raises."""
    name = getattr(container, "name", "?")
    try:
        raw = container.logs(tail=MAX_LOG_LINES)
    except _DAEMON_ERRORS as e:
        logger.warning("Log retrieval failed for %s: %s", name, e)
        return f"<log retrieval failed: {e}>\n"
    return _truncate(raw.decode("utf-8", errors="replace"))


def _safe_remove(container: Any) -> None:
    """Remove a container without ever masking the caller's result."""
    try:
        container.remove(force=True)
    except docker.errors.NotFound:
        pass
    except _DAEMON_ERRORS as e:
        logger.warning(
            "Removal failed for %s: %s", getattr(container, "name", "?"), e,
        )


def _stop_and_remove(container: Any) -> None:
    """Stop then force-remove a long-lived container. Never raises."""
    name = getattr(container, "name", "?")
    try:
        container.stop(timeout=5)
    except docker.errors.NotFound:
        return
    except _DAEMON_ERRORS as e:
        logger.warning("Stop failed for %s: %s", name, e)
    _safe_remove(container)


def _created_at_monotonic(container: Any) -> float:
    """Return a `time.monotonic()`-based creation stamp for `container`.

    `monotonic()` is per-process and cannot survive a restart, so an adopted
    container would otherwise be treated as brand new — resetting its
    SESSION_MAX_AGE budget on every redeploy and defeating the 24h cap. Docker
    records the real creation time, so derive the age from that and convert it
    into the monotonic clock once.
    """
    raw = (getattr(container, "attrs", None) or {}).get("Created")
    if not raw:
        return time.monotonic()
    text = str(raw).replace("Z", "+00:00")
    match = _ISO_FRAC_RE.match(text)
    if match:
        text = f"{match['head']}.{match['frac'][:6]}{match['tz']}"
    try:
        created = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        logger.warning(
            "Unparseable Created timestamp on %s: %r",
            getattr(container, "name", "?"), raw,
        )
        return time.monotonic()
    age_s = (datetime.now(timezone.utc) - created).total_seconds()
    # Clamp negative ages (host clock skew) so a session can never look
    # "created in the future" and dodge the reaper forever.
    return time.monotonic() - max(age_s, 0.0)


# ── Repo clone (session mode only) ────────────────────────────────────────
def _repo_allowlist() -> tuple[str, ...]:
    """Slugs this deployment may clone. Format: comma-separated owner/name.

    Deployment data from PI_REPO_ALLOWLIST, never model input. Empty means
    cloning is disabled, which is the safe default: without an allowlist a
    caller could name any repository on the instance and have this service
    clone it with a real credential.
    """
    raw = os.environ.get("PI_REPO_ALLOWLIST", "")
    return tuple(s.strip() for s in raw.split(",") if s.strip())


def _validate_repo(repo: str | None) -> str | None:
    """Return `repo` if allowlisted; raise 422 otherwise.

    The rejection deliberately does not echo the allowlist. A caller has no
    need to enumerate it, and the detail string is shown to an LLM.
    """
    if repo is None:
        return None
    allowed = _repo_allowlist()
    if not allowed:
        raise HTTPException(
            status_code=422,
            detail="No repositories are allowed: PI_REPO_ALLOWLIST is empty.",
        )
    if repo not in allowed:
        raise HTTPException(
            status_code=422, detail="Repository is not in the allowlist.",
        )
    return repo


# Run as `sh -c SCRIPT pi-clone <slug>`: $0 is the label, $1 the slug.
_CLONE_SCRIPT = r"""
set -eu
slug="$1"
name="${slug##*/}"
dest="/workspace/$name"
export GIT_TERMINAL_PROMPT=0
# TLS verification stays ON. The internal CA is installed into the system
# trust store by pi-sandbox.Dockerfile, so git resolves it on its own. The
# previous GIT_SSL_NO_VERIFY=true workaround is deliberately gone: it disabled
# verification for EVERY host, including the provider endpoints.
export GIT_SSL_NO_VERIFY=false
# Optional extra trust anchor, for a deployment whose private CA is not baked
# into the image. Empty means the system store, the normal case.
if [ -n "${GIT_CA_FILE:-}" ] && [ -r "$GIT_CA_FILE" ]; then
    export GIT_SSL_CAINFO="$GIT_CA_FILE"
fi
# The credential helper is evaluated at CALL time and reads $GIT_TOKEN from the
# environment, so the token never reaches .git/config, `git remote -v`, or any
# diff. Embedding it in the URL would leak it into all three.
git config --global --replace-all credential.helper \
  '!f() { echo username=x-access-token; echo "password=$GIT_TOKEN"; }; f'
# FAIL FAST if the helper did not install. The helper write above can fail
# (no HOME, read-only config) and git would then fall through to an
# interactive prompt with no TTY, producing a misleading "could not read
# Username" instead of naming the real cause.
helper="$(git config --global --get credential.helper 2>/dev/null || true)"
if [ -z "$helper" ]; then
    echo "ERROR: credential helper not installed; git cannot authenticate" >&2
    echo "       (HOME=${HOME:-UNSET}) - the clone would prompt and fail" >&2
    exit 1
fi
if [ -d "$dest/.git" ]; then
    echo "REPO_FETCH $slug"
    git -C "$dest" fetch --prune origin
else
    echo "REPO_CLONE $slug"
    mkdir -p /workspace
    # The base URL arrives as $2 and is never interpolated into this constant,
    # so no deployment hostname lives in the source. Verification stays ON.
    git clone --no-single-branch "${2%/}/${slug}.git" "$dest"
fi
git -C "$dest" log --oneline -1
echo "REPO_READY $dest"
"""


def _ensure_repo(container: Any, repo: str) -> None:
    """Clone (or fetch) `repo` into /workspace/<name> before `pi` starts.

    Idempotent: a second run on the same session fetches rather than cloning,
    so a follow-up task sees upstream changes without losing local work.

    The token is passed PER-EXEC rather than read from the container's env, so
    a container ADOPTED from a previous runner process (created before this
    deploy) still works.
    """
    token = os.environ.get("GIT_TOKEN")
    if not token:
        raise HTTPException(
            status_code=503,
            detail="GIT_TOKEN is not configured; cannot clone repositories.",
        )
    # Fail loudly rather than cloning from a guessed host. A default here would
    # be a second place that decides where code comes from.
    problem = _base_url_problem(_GITEA_BASE_URL)
    if problem:
        raise HTTPException(status_code=503, detail=problem)
    # HOME and PATH MUST be carried across. exec_run(environment=...) REPLACES
    # the container environment rather than merging into it, so passing only the
    # two git vars strips HOME. `git config --global` then dies with
    #     fatal: $HOME not set
    # and the credential helper is never installed, so git falls back to an
    # interactive prompt and the clone fails with
    #     could not read Username ... No such device or address
    # That is byte-for-byte the failure this produced in production.
    env = {
        "GIT_TOKEN": token,
        "GIT_CA_FILE": _GIT_CA_FILE,
        "HOME": "/root",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
    }
    res = container.exec_run(
        # argv: $0 is the label, $1 the slug, $2 the base URL.
        ["timeout", "300", "sh", "-c", _CLONE_SCRIPT, "pi-clone", repo,
         _GITEA_BASE_URL],
        workdir="/workspace",
        environment=env,
    )
    if res.exit_code != 0:
        out = _truncate((res.output or b"").decode("utf-8", errors="replace"))
        logger.error(
            "Clone failed for %s (exit=%d): %s", repo, res.exit_code, out[-500:],
        )
        # The DETAIL used to be just the exit code, which threw away the only
        # evidence the caller could act on. "exit 128" says nothing; the last
        # line of git's stderr names the cause. Redacted because git output can
        # echo a URL, and this string reaches an LLM-driven client.
        tail = _redact(out[-400:]) if out else "<no output>"
        raise HTTPException(
            status_code=502,
            detail=(
                f"Repository clone failed (exit {res.exit_code}): {tail}"
            ),
        )
    logger.info("Repo ready in session: %s", repo)


# ── One-shot mode ─────────────────────────────────────────────────────────
def _run_pi_one_shot(req: RunRequest) -> RunResponse:
    """Create a fresh pi-sandbox container, run `pi -p`, capture output."""
    client = get_client()
    # No session_id: a one-shot must not share Pi's session store with anything.
    cmd = _build_pi_cmd(req.task, req.provider, req.model)

    # Log the length, not the text: a task may contain credentials or snippets
    # the operator does not expect to find in the service log.
    logger.info(
        "One-shot run: task_len=%d, provider=%s, model=%s, timeout=%d",
        len(req.task), req.provider, req.model, req.timeout,
    )

    container = client.containers.create(
        image=PI_IMAGE,
        command=cmd,
        environment=_env_for_sandbox(),
        name=f"pi-one-shot-{uuid.uuid4().hex[:12]}",
        network=LLAMA_NETWORK,
        # The deadline label is what makes the "left running" case below
        # reclaimable by the reaper instead of leaking until a restart.
        labels={
            **LABEL_ONE_SHOT,
            LABEL_DEADLINE: str(
                int(time.time() + req.timeout + WAIT_GRACE_S)
            ),
        },
    )

    # Decides whether the `finally` below is allowed to delete the container.
    # A lost connection must not destroy a container that is still working.
    reclaim = True
    start = time.monotonic()
    timed_out = False
    exit_code = -1
    try:
        container.start()
        try:
            result = container.wait(timeout=req.timeout + WAIT_GRACE_S)
            status = result.get("StatusCode")
            exit_code = int(status) if status is not None else -1
        except _DAEMON_ERRORS:
            # A wait failure says nothing about the outcome — see the module
            # docstring. Ask the daemon which of the three cases this is.
            try:
                container.reload()
            except _DAEMON_ERRORS:
                # State unknowable: let the generic handler report the failure
                # rather than guess and risk mislabelling the outcome. Do not
                # delete the container either — we cannot tell whether work is
                # still in flight.
                reclaim = False
                raise

            elapsed = time.monotonic() - start
            deadline = req.timeout + WAIT_GRACE_S

            if container.status == "running" and elapsed >= deadline - DEADLINE_SLACK_S:
                # Case 1: still working, and we are at/over the deadline.
                timed_out = True
                exit_code = -1
                logger.warning(
                    "One-shot %s exceeded %ds; killing (elapsed=%.1fs).",
                    container.name, req.timeout, elapsed,
                )
                with contextlib.suppress(*_DAEMON_ERRORS):
                    container.kill()
            elif container.status == "running":
                # Case 3: a transport blip well before the deadline. The task
                # is fine — do NOT kill it, and do not report it as a result.
                # The deadline label lets the reaper remove it later, so it
                # cannot keep calling a paid provider with no output consumer.
                logger.error(
                    "Lost the wait on %s after %.1fs (deadline %ds); container "
                    "is still running and left for the reaper.",
                    container.name, elapsed, deadline,
                )
                reclaim = False
                raise HTTPException(
                    status_code=500,
                    detail=(
                        f"Lost connection to the run after {elapsed:.0f}s "
                        f"(deadline {deadline}s); the container is still running "
                        "and was left in place. It will be reaped at its "
                        "deadline; the output is not recoverable."
                    ),
                )
            else:
                # Case 2: it exited while the wait was stalled. The run
                # completed; recover the real status instead of raising, or the
                # cleanup below would delete a successful run's logs.
                state = (container.attrs or {}).get("State") or {}
                code = state.get("ExitCode")
                exit_code = int(code) if code is not None else -1
                timed_out = False
                logger.info(
                    "One-shot %s exited while waiting; recovered exit_code=%d.",
                    container.name, exit_code,
                )

        duration = time.monotonic() - start
        # Logs must be read BEFORE the finally-block removes the container —
        # this is what makes a timed-out run diagnosable.
        stdout = _snapshot_logs(container)

        logger.info(
            "One-shot done: exit_code=%d, duration=%.1fs, timed_out=%s",
            exit_code, duration, timed_out,
        )
        return RunResponse(
            exit_code=exit_code,
            stdout=stdout,
            stderr="",
            duration_s=round(duration, 2),
            timed_out=timed_out,
        )
    except _DAEMON_ERRORS as e:
        logger.error("One-shot run failed: %s", e)
        raise HTTPException(status_code=500, detail=f"One-shot run failed: {e}") from e
    finally:
        if reclaim:
            _safe_remove(container)
        else:
            # Left running on purpose. `_sweep_stray_one_shots` finds it by
            # label once its deadline passes, so it cannot leak indefinitely.
            logger.warning(
                "Leaving %s running for the reaper.", getattr(container, "name", "?"),
            )


# ── Session mode ──────────────────────────────────────────────────────────
@dataclass
class _Session:
    container: Any          # None only while creation is in flight
    created_at: float       # set once, never updated (SESSION_MAX_AGE basis)
    last_active: float
    busy: bool = False


def _adopt_session_container(sid: str) -> Any:
    """Take over a container that survived a previous runner process.

    Returns it only if it is genuinely running. Returning a container that
    cannot start would wedge the id permanently: every run fails in exec_run,
    the entry is popped, the next create hits 409, and we adopt the same dead
    container forever. Removing it frees the name for a clean retry.
    """
    client = get_client()
    container = client.containers.get(f"pi-session-{sid}")
    try:
        if container.status != "running":
            container.start()
        container.reload()
        if container.status != "running":
            raise docker.errors.APIError(
                f"{container.name} is {container.status}, not running"
            )
    except _DAEMON_ERRORS:
        logger.error("Adopt failed for %s; removing the broken container.", sid)
        _safe_remove(container)
        raise
    logger.info("Adopted existing session container %s", container.name)
    return container


def _create_session_container(sid: str) -> Any:
    """Start a persistent container for `sid`, adopting a leftover if present."""
    client = get_client()
    try:
        container = client.containers.create(
            image=PI_IMAGE,
            command=["tail", "-f", "/dev/null"],
            environment=_env_for_sandbox(),
            name=f"pi-session-{sid}",
            network=LLAMA_NETWORK,
            labels={**LABEL_SESSION, "pi-session-id": sid},
        )
        container.start()
        return container
    except docker.errors.APIError as e:
        # 409: a container with this name survived a previous runner process.
        # Adopt it instead of wedging the session id forever. `_closing` is
        # checked by the caller so we never adopt one that is being removed.
        if e.status_code != 409:
            raise
        return _adopt_session_container(sid)


def _acquire_session(sid: str) -> "_Session":
    """Get (or create) the session and mark it busy for the duration of a run.

    Container creation happens OUTSIDE `_session_lock`: `create`+`start` is
    slow daemon I/O and would otherwise stall /pi/sessions and every other
    session request for its duration. A reservation entry (container=None,
    busy=True) blocks a concurrent create for the same id instead.
    """
    with _session_lock:
        if sid in _closing:
            # A close/reap is mid-removal. Surface a retryable 409 instead of
            # creating a container, hitting 409 on the name, and adopting the
            # very container that is about to be stopped and removed.
            raise HTTPException(
                status_code=409,
                detail=f"Session '{sid}' is being closed; retry shortly.",
            )

        session = _sessions.get(sid)
        if session is not None:
            if session.busy or session.container is None:
                raise HTTPException(
                    status_code=409,
                    detail=f"Session '{sid}' is already running a task.",
                )
            session.busy = True
            session.last_active = time.monotonic()
            return session

        if len(_sessions) >= MAX_CONCURRENT_SESSIONS:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Session limit reached ({MAX_CONCURRENT_SESSIONS}); "
                    "close an idle session and retry."
                ),
            )
        now = time.monotonic()
        session = _Session(
            container=None, created_at=now, last_active=now, busy=True,
        )
        _sessions[sid] = session

    try:
        session.container = _create_session_container(sid)
    except BaseException as e:
        # Release the reservation so the id is immediately usable again.
        with _session_lock:
            _sessions.pop(sid, None)
        if isinstance(e, docker.errors.DockerException):
            logger.error("Session create failed for %s: %s", sid, e)
            raise HTTPException(
                status_code=500, detail=f"Session create failed: {e}",
            ) from e
        raise
    logger.info("Created session container for sid=%s", sid)
    return session


def _release_session(sid: str, expected: "_Session | None" = None) -> None:
    """Clear the busy flag and only then record activity.

    `expected` guards against a stale release: `_run_pi_session` may pop the
    entry on a fatal daemon error and *then* release in its `finally`, so a
    concurrent run for the same id may already have installed a replacement.
    Blindly clearing `busy` there would hand the same container to a second
    caller and let two `pi` processes corrupt one /workspace.
    """
    with _session_lock:
        live = _sessions.get(sid)
        if live is None:
            return
        if expected is not None and live is not expected:
            logger.info(
                "Skipping release for %s: the session was replaced.", sid,
            )
            return
        live.busy = False
        live.last_active = time.monotonic()


def _remove_tombstoned(sid: str | None, container: Any) -> None:
    """Remove a container whose `sid` is ALREADY tombstoned. Never raises.

    The tombstone must have been applied in the same critical section that
    claimed the victim — see the module docstring. This helper only performs
    the slow I/O and drops the tombstone afterwards.
    """
    try:
        _stop_and_remove(container)
    finally:
        if sid:
            with _session_lock:
                _closing.discard(sid)


def _run_pi_session(req: RunRequest) -> RunResponse:
    """Reuse (or create) a persistent pi-session container."""
    sid = req.session_id
    if not sid:
        raise HTTPException(status_code=422, detail="session_id required.")

    # `session` stays None until acquisition succeeds, so the `finally` can tell
    # "never acquired" (nothing to release) from "acquired" (release it).
    session: _Session | None = None
    try:
        # Inside the try: acquisition can raise, and an exception between
        # acquire and here would otherwise leave busy=True forever.
        session = _acquire_session(sid)

        # Before `pi` runs, so the agent never starts against a half-populated
        # workspace. Reuses the busy flag this call already holds, so there is
        # no new lock and -- importantly -- no new removal path: the
        # tombstone invariant in the module docstring is untouched.
        if req.repo:
            _ensure_repo(session.container, req.repo)

        # `timeout` runs inside the container, so a hung `pi` cannot outlive the
        # request: coreutils exits 124 (TERM) / 137 (KILL) when the deadline
        # hits. This is *not* the only bound in play — the docker client's
        # DOCKER_TIMEOUT_S caps the HTTP call itself.
        #
        # `session_id` is forwarded so Pi resumes its conversation, not just
        # its filesystem. `--` in the pi argv is what stops the task being
        # parsed as an option; `timeout`'s own option parsing stops at the
        # `pi` token.
        cmd = [
            "timeout", "--signal=TERM", "--kill-after=10", str(req.timeout),
            *_build_pi_cmd(req.task, req.provider, req.model, session_id=sid),
        ]

        start = time.monotonic()
        # NOTE: exec_run is not streamed, so this holds one HTTP response open
        # for the whole run (start + poll). Only DOCKER_TIMEOUT_S bounds it.
        result = session.container.exec_run(cmd, workdir="/workspace")
        duration = time.monotonic() - start
        exit_code = result.exit_code
        output = result.output or b""
        stdout = _truncate(output.decode("utf-8", errors="replace"))
        # 124 = coreutils timeout fired (TERM). 137 = SIGKILL, which is either
        # our own --kill-after escalation or an OOM kill of the container; the
        # two are not distinguishable from the exec status alone, so 137 is
        # reported as a timeout (the escalation case, the common one).
        timed_out = exit_code in (124, 137)

        logger.info(
            "Session run done: sid=%s, exit_code=%d, duration=%.1fs, timed_out=%s",
            sid, exit_code, duration, timed_out,
        )
        return RunResponse(
            exit_code=exit_code,
            stdout=stdout,
            stderr="",
            duration_s=round(duration, 2),
            timed_out=timed_out,
        )
    except _DAEMON_ERRORS as e:
        # The entry is dropped so a container reaped mid-exec does not wedge the
        # id — but ONLY if the container is actually gone. A transport blip
        # leaves a perfectly healthy container running, and discarding the entry
        # would cost the caller their /workspace and Pi history for no reason.
        keep = False
        if session is not None and session.container is not None:
            with contextlib.suppress(*_DAEMON_ERRORS):
                session.container.reload()
                keep = session.container.status == "running"
        if keep:
            logger.error(
                "Session exec failed but container is still running; "
                "keeping session %s: %s", sid, e,
            )
            raise HTTPException(status_code=500, detail=str(e)) from e
        with _session_lock:
            _sessions.pop(sid, None)
        logger.error("Session exec failed; dropping session %s: %s", sid, e)
        raise HTTPException(status_code=500, detail=str(e)) from e
    finally:
        if session is not None:
            # Identity-checked: the entry may already be gone, or may now be a
            # different session object owned by another in-flight request.
            _release_session(sid, expected=session)


# ── Reaping ───────────────────────────────────────────────────────────────
def _reap_idle_sessions() -> None:
    """Remove sessions that are idle or over their age budget.

    Pops and tombstones in ONE critical section — see the module docstring.
    """
    now = time.monotonic()
    claimed: list[tuple[str, "_Session"]] = []

    with _session_lock:
        for sid, session in list(_sessions.items()):
            if session.busy or session.container is None:
                continue  # never reap a session mid-run or mid-create
            idle = now - session.last_active
            age = now - session.created_at
            if idle > SESSION_IDLE_TIMEOUT or age > SESSION_MAX_AGE:
                claimed.append((sid, _sessions.pop(sid)))
                _closing.add(sid)

    # Slow daemon I/O happens outside the lock.
    for sid, session in claimed:
        logger.info("Reaping session %s", sid)
        _remove_tombstoned(sid, session.container)


def _sweep_orphan_sessions() -> None:
    """Remove session containers whose `_sessions` entry is gone.

    Covers a failed `close_session` removal and containers left by a previous
    runner process.

    Each orphan is tombstoned in the SAME lock acquisition that decides it is an
    orphan. Without that, a concurrent request for the same id finds no entry
    and no tombstone, reserves a slot, calls `create` -> 409 (the orphan still
    owns the name) -> adopts the container about to be dropped, and execs into
    it. Tombstoning in a later acquisition only narrows that window; it does
    not close it. This is the #17/#19 defect, and it lived here until v1.1.7.

    A container mid-create cannot be caught here: `_acquire_session` inserts the
    `_sessions[sid]` entry *before* asking the daemon to create anything, so a
    container this process is creating is already in `_sessions`. Containers
    from a previous process are not, which is precisely why they land here.
    """
    try:
        containers = get_client().containers.list(
            all=True, filters={"label": f"{LABEL_KEY}=session"},
        )
    except _DAEMON_ERRORS as e:
        logger.warning("Orphan sweep skipped: %s", e)
        return

    confirmed: list[tuple[str | None, Any]] = []
    with _session_lock:
        for container in containers:
            sid = (container.labels or {}).get("pi-session-id")
            if sid and (sid in _sessions or sid in _closing):
                continue
            if sid:
                # Atomic with the membership check above — this is the fix.
                _closing.add(sid)
            confirmed.append((sid, container))

    for sid, container in confirmed:
        logger.info("Reaping orphaned session container %s", container.name)
        _remove_tombstoned(sid, container)


def _sweep_stray_one_shots() -> None:
    """Remove one-shot containers that outlived their deadline.

    `_run_pi_one_shot` leaves a container running when the wait fails while the
    container is still healthy (a transport blip). That is correct — killing it
    would destroy live work — but nothing was reclaiming it. The container kept
    running `pi` against a paid provider with no consumer for its output, and
    only the NEXT runner restart would clean it up.

    The deadline label set at creation turns "a container is running" into
    "a container is running and it is past its deadline", which is enough to
    reclaim safely.
    """
    try:
        containers = get_client().containers.list(
            all=True, filters={"label": f"{LABEL_KEY}=one-shot"},
        )
    except _DAEMON_ERRORS as e:
        logger.warning("One-shot sweep skipped: %s", e)
        return

    now_epoch = time.time()
    for container in containers:
        raw = (container.labels or {}).get(LABEL_DEADLINE)
        deadline: int | None
        try:
            deadline = int(raw) if raw is not None else None
        except (TypeError, ValueError):
            deadline = None

        if deadline is not None:
            if now_epoch < deadline + WAIT_GRACE_S:
                continue  # still legitimately working
            logger.info(
                "Reaping stray one-shot container %s (deadline passed %.0fs ago)",
                container.name, now_epoch - deadline,
            )
            _safe_remove(container)
            continue

        # No readable deadline (an older image, or a hand-made container).
        # Cannot judge by deadline, so bound it by the largest run we permit.
        # Never removes recent work.
        age = time.monotonic() - _created_at_monotonic(container)
        if age < MAX_TIMEOUT + WAIT_GRACE_S:
            continue
        logger.info(
            "Reaping deadline-less one-shot container %s (age=%.0fs)",
            container.name, age,
        )
        _safe_remove(container)


def _cleanup_sessions() -> None:
    """One reaper iteration: sessions, then orphans, then stray one-shots."""
    _reap_idle_sessions()
    _sweep_orphan_sessions()
    _sweep_stray_one_shots()


# ── Startup / background reaper ───────────────────────────────────────────
def _reconcile_at_startup() -> None:
    """Reap orphaned one-shots and adopt surviving session containers.

    In-process state does not survive a restart, so without this every
    pi-session-* container becomes invisible and every re-create for the same
    id fails with 409 — wedging that session id permanently.
    """
    client = get_client()
    try:
        containers = client.containers.list(all=True, filters={"label": LABEL_KEY})
    except _DAEMON_ERRORS as e:
        logger.warning("Startup reconciliation skipped: %s", e)
        return

    for container in containers:
        labels = container.labels or {}
        kind = labels.get(LABEL_KEY)
        if kind == "one-shot":
            logger.info("Reaping orphaned one-shot container %s", container.name)
            _safe_remove(container)
            continue
        sid = labels.get("pi-session-id")
        if kind != "session" or not sid:
            continue
        # Derive the real age from the daemon rather than assuming "now":
        # otherwise every redeploy resets the 24h clock and an actively used
        # session outlives SESSION_MAX_AGE indefinitely.
        created_at = _created_at_monotonic(container)
        with _session_lock:
            if sid in _sessions:
                continue
            _sessions[sid] = _Session(
                container=container, created_at=created_at,
                last_active=time.monotonic(),
            )
        logger.info(
            "Adopted session container %s (sid=%s, age=%.0fs)",
            container.name, sid, time.monotonic() - created_at,
        )
        with contextlib.suppress(*_DAEMON_ERRORS):
            if container.status != "running":
                container.start()


async def _reaper_loop() -> None:
    while True:
        await asyncio.sleep(REAP_INTERVAL_S)
        try:
            # to_thread: docker stop/remove are blocking calls and must not
            # stall the event loop.
            await asyncio.to_thread(_cleanup_sessions)
        except Exception:  # noqa: BLE001 - the loop must never die
            logger.exception("Reaper iteration failed")


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info("pi-agent-runner starting (v%s)", VERSION)
    logger.info(
        "Allowlist: provider(s)=%s; models=%s; default=%s/%s",
        ",".join(_ALLOWED_PROVIDERS),
        "; ".join(f"{p}={','.join(m)}" for p, m in _ALLOWED_MODELS.items()),
        DEFAULT_PROVIDER_NAME, DEFAULT_MODEL_NAME,
    )

    # Surface a misconfigured deploy instead of letting every run fail with an
    # opaque provider auth error. Log and continue: the session and adoption
    # paths are still useful, and /health should reflect the daemon, not this.
    if not _env_for_sandbox():
        logger.error(
            "No provider API key in the environment (%s) — every run will fail "
            "to authenticate. Check the deploy secrets.", ", ".join(_ENV_KEYS),
        )

    try:
        get_client().ping()
        logger.info("Docker socket reachable.")
    except _DAEMON_ERRORS as e:
        logger.error("Docker socket not reachable: %s", e)

    try:
        await asyncio.to_thread(_reconcile_at_startup)
    except Exception:  # noqa: BLE001 - never block startup on reconciliation
        logger.exception("Startup reconciliation failed")

    reaper = asyncio.create_task(_reaper_loop())
    try:
        yield
    finally:
        reaper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reaper


app = FastAPI(title="pi-agent-runner", version=VERSION, lifespan=lifespan)


# ── Routes ────────────────────────────────────────────────────────────────
@app.get("/health")
def health() -> Any:
    """Report daemon reachability, alongside the model allowlist.

    Uses the short-timeout client: on the shared client a wedged daemon would
    block this for DOCKER_TIMEOUT_S (~31 min), so a healthcheck could never
    observe a failure — only hang.

    The allowlist is echoed deliberately. models.json and settings.json are
    baked into the pi-sandbox image while these constants live in the runner
    image, so the two can drift across a partial deploy. Reporting them makes
    that visible from a single call instead of from a failed run later.
    """
    try:
        _get_health_client().ping()
        return {
            "status": "ok",
            "docker": "reachable",
            "version": VERSION,
            "provider": DEFAULT_PROVIDER_NAME,
            "model": DEFAULT_MODEL_NAME,
            "allowed_providers": list(_ALLOWED_PROVIDERS),
            "allowed_models": {p: list(m) for p, m in _ALLOWED_MODELS.items()},
            # COUNT, not the names: the allowlist is deployment data and this
            # response is visible to any caller. The count is enough to answer
            # "is cloning configured at all?".
            "repos_configured": len(_repo_allowlist()),
            # Whether a clone could actually run. A non-empty allowlist with no
            # base URL is a misconfiguration that would fail at run time, so it
            # is visible here instead.
            # NOT a bare bool. A base URL that is present but malformed is
            # unusable, and the boolean reported True through an entire outage
            # because it only checked the value was non-empty.
            "repo_clone_ready": (
                bool(_repo_allowlist())
                and _base_url_problem(_GITEA_BASE_URL) is None
            ),
            "repo_clone_problem": _base_url_problem(_GITEA_BASE_URL),
        }
    except _DAEMON_ERRORS as e:
        logger.warning("Health probe failed: %s", e)
        return JSONResponse(
            status_code=503,
            content={"status": "error", "detail": str(e), "version": VERSION},
        )


@app.post("/pi/run", response_model=RunResponse)
def run_pi(req: RunRequest) -> RunResponse:
    # Allowlist enforcement, and the provider/model all-or-nothing rule.
    # Raises 422 for anything not explicitly permitted.
    req.provider, req.model = _validate_provider_model(req.provider, req.model)
    req.repo = _validate_repo(req.repo)

    # Session-only on purpose. One-shot mode builds `pi-one-shot-*` with a fixed
    # command at create time and removes it immediately after reading logs, so
    # adding a clone there means restructuring the path that carries the
    # deadline label and reclaim logic. Not worth it until the clone proves
    # useful in session mode.
    if req.repo and not req.session_id:
        raise HTTPException(
            status_code=422,
            detail="`repo` requires `session_id`; cloning is session-mode only.",
        )

    if req.session_id:
        return _run_pi_session(req)

    # Bound one-shot concurrency: each run spawns a container, and these
    # routes are sync, so an unbounded fan-out would exhaust the anyio worker
    # pool and take /health down with it.
    if not _run_gate.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail=f"Too many concurrent runs (limit {MAX_CONCURRENT_RUNS}).",
        )
    try:
        return _run_pi_one_shot(req)
    finally:
        _run_gate.release()


@app.post("/pi/session/close/{session_id}")
def close_session(session_id: str) -> dict[str, Any]:
    with _session_lock:
        if session_id in _closing:
            raise HTTPException(
                status_code=409,
                detail="Session is already being closed.",
            )
        session = _sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found.")
        if session.busy or session.container is None:
            raise HTTPException(
                status_code=409,
                detail="Session is running a task; retry when it finishes.",
            )
        # Pop and tombstone in one critical section. The entry is dropped so the
        # id is reusable even if removal fails; the orphan sweep reclaims the
        # container by label.
        _sessions.pop(session_id, None)
        _closing.add(session_id)
    _remove_tombstoned(session_id, session.container)
    return {"status": "closed", "session_id": session_id}


@app.get("/pi/sessions")
def list_sessions() -> dict[str, Any]:
    now = time.monotonic()
    with _session_lock:
        items = [
            {
                "session_id": sid,
                "idle_seconds": round(now - s.last_active, 1),
                "age_seconds": round(now - s.created_at, 1),
                "busy": s.busy,
                "ready": s.container is not None,
            }
            for sid, s in _sessions.items()
        ]
    return {"sessions": items, "limit": MAX_CONCURRENT_SESSIONS}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8092)
