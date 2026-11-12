# pi-sandbox — minimal container for the Pi coding agent (pi.dev)
# Whole `pi` process runs inside; only /workspace is mounted at runtime.
# Built by the deploy workflow and used by pi-agent-runner for one-shot runs.
FROM node:24-bookworm-slim

# Minimal toolset Pi actually needs for coding work.
#
# curl is here because it was NOT here before, and that mattered: reachability
# was being probed with node's net/tls modules because neither curl nor ip was
# installed. An agent that cannot make an HTTP request improvises with node or
# python, which is both slower and less predictable than the tool everyone
# expects. git is required for cloning the project under test; ripgrep backs
# grep; python3 is required by many projects' own checks.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash ca-certificates git ripgrep curl \
      build-essential make \
 && rm -rf /var/lib/apt/lists/*

# ── Python toolchain, on purpose ──────────────────────────────────────
#
# This image originally installed only Debian python3 (3.11) and no pip.
# Measured consequence: a project requiring >=3.12 with a `make check`
# target could not be verified at all. An agent in that position either
# claims a check passed that it never ran, or reports "cannot run". Only
# one of those is honest and neither is useful.
#
# The fix is uv, not apt python3:
#   - Debian bookworm ships Python 3.11 only; there is NO python3.12
#     package, so the version most projects pin cannot come from apt.
#   - uv fetches a standalone CPython build, so any version a task needs
#     is available without touching the system interpreter.
#   - uv venv does not need ensurepip, which is what made
#     `python3 -m venv` fail here ("ensurepip is not available").
#
# /usr/local/bin is on PATH for the non-root runner user. Managed Pythons
# live under /opt so sessions share one 33 MB download instead of
# re-fetching it per container.
ENV UV_INSTALL_DIR=/usr/local/bin \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    UV_LINK_MODE=copy

RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
 && uv --version \
 && uv python install 3.12 3.13 \
 && uv run --python 3.12 python -c 'import sqlite3,sys; print("py", sys.version.split()[0], "sqlite", sqlite3.sqlite_version)' \
 && uv python find 3.12

# ── the internal CA, in the TRUST STORE ──────────────────────────────────
#
# gitea.example.internal is signed by a PRIVATE CA that is not in the Debian bundle,
# so `git clone` failed with "server verification failed: certificate signer
# not trusted". The clone helper worked around it with GIT_SSL_NO_VERIFY=true,
# which is not a fix: it disables verification for EVERY host, including the
# provider endpoints.
#
# Installed into the system trust store rather than passed as a path in an env
# var, so there is ONE source of truth. Measured: git resolves
# /etc/ssl/certs/ca-certificates.crt on its own, so this needs no setting and
# no change to the clone script.
#
# The chain is a COPY of pi-harness's gateway/nuc-ca-chain.pem (2197 bytes,
# 2 certificates: leaf + intermediate). Kept as a copy rather than a submodule
# because a build context cannot reach another repo.
#
# WHY NOT sslVerify=false in git config: that trades the control for a test
# convenience, exactly as pi-harness D-34 refused to do for the socket allowlist.
# NOT a COPY. A `COPY` of a missing file fails the build, and this chain is
# export-ignored, so a checkout of the PUBLIC snapshot does not contain it.
# Staging is therefore a shell test: present => trust the private CA (the
# deployment case); absent => build normally against the public root store (the
# case for anyone building from the public repo).
#
# The build context is copied to /tmp/buildctx for the test. It is removed in the
# same layer, so nothing extra survives into the image.
COPY . /tmp/buildctx
RUN set -eu; \
    CERT=/usr/local/share/ca-certificates/nuc-ca-chain.crt; \
    if [ -f /tmp/buildctx/nuc-ca-chain.pem ]; then \
        cp /tmp/buildctx/nuc-ca-chain.pem "$CERT"; \
    fi; \
    rm -rf /tmp/buildctx; \
    if [ -f "$CERT" ]; then \
        test -s "$CERT"; \
        before="$(grep -c 'BEGIN CERTIFICATE' /etc/ssl/certs/ca-certificates.crt)"; \
        want="$(grep -c 'BEGIN CERTIFICATE' "$CERT")"; \
        update-ca-certificates; \
        after="$(grep -c 'BEGIN CERTIFICATE' /etc/ssl/certs/ca-certificates.crt)"; \
        echo "trust store: before=$before after=$after chain=$want"; \
        if [ "$after" -ne "$((before + want))" ]; then \
            echo "ERROR: the local CA was NOT merged into the trust store" >&2; \
            echo "expected $((before + want)), got $after -- every clone would fail" >&2; \
            exit 1; \
        fi; \
        echo "local CA merged into the system trust store"; \
    else \
        echo "no local CA in the build context; system trust store only"; \
    fi

# Pin Pi exactly. `npm install -g` with no version floated the sandbox to
# whatever was latest at build time, so two deploys of the same commit could
# run different agents. 1.5.0 is the version pi-subagents 0.87.0 was verified
# against; the extension API is versioned and the peer floor is >=1.2.0.
#
# --ignore-scripts is deliberate and unchanged: lifecycle scripts are not
# needed for the CLI and running them at build time is free supply-chain risk.
RUN npm install -g --ignore-scripts @earendil-works/pi-coding-agent@1.5.0

# Bake the static Pi config into the image. pi reads these from
# /root/.pi/agent/. Baking them in (instead of bind-mounting at runtime)
# avoids the runc "not a directory" / EISDIR mount failures that happen when
# the runner passes container-local paths as host bind sources.
#
# settings.json carries the `packages` list and the per-role `subagents`
# overrides, so it must be copied BEFORE the package install below: the install
# resolves the entry that is already there.
COPY models.json settings.json /root/.pi/agent/

# The provider-fallback extension + its config, loaded by pi on startup. A
# single .ts file that only imports host-provided @earendil-works/* modules, so
# it loads via jiti with no extra npm install.
COPY extensions/provider-fallback.ts /root/.pi/agent/extensions/provider-fallback.ts
COPY provider-fallback.json /root/.pi/agent/extensions/provider-fallback.json

# Subagent role definitions. `verifier` exists because the shipped `reviewer`
# has no bash tool (tools: read, grep, find, ls, watchdog_diff, ...) and is
# therefore a judgment layer rather than a CI gate — it reports the command a
# supervisor must run instead of running it. `programmer` encodes plan
# adherence. Both are project-scope-free: they are baked here so every run has
# them without seeding /workspace.
COPY agents/ /root/.pi/agent/agents/

# Install the subagent runtime at a PINNED version.
#
# This line is load-bearing in a non-obvious way. pi-subagents declares its Pi
# API packages as peerDependenciesMeta{optional:true}, so npm installs ZERO
# peers. Pi itself is installed GLOBALLY (-g), so Node cannot resolve
# @earendil-works/* from /root/.pi/agent/npm/. The result is not an install
# error — pi loads extensions leniently, prints nothing, not even with
# --verbose, and the model simply never sees the `subagent` tool:
#
#     IMPORT FAIL: Cannot find package '@earendil-works/pi-coding-agent'
#       imported from .../pi-subagents/bundle/chunk-*.js
#
# NODE_PATH below is the fix. Verified: with it, a `pi -p` run reports
# TOOLS_SEEN=read,bash,edit,write,subagent,contact_supervisor,bg_wait,...
# and the tool is available on the FIRST request (no toolActivation round trip).
# Without it, TOOLS_SEEN=read,bash,edit,write and no error anywhere.
#
# Do not add one of these without the other. `pi install` is used rather than
# `npm install -g` because pi resolves packages from its own store
# (/root/.pi/agent/npm/node_modules) and records the spec in settings.json.
RUN pi install npm:pi-subagents@0.87.0
ENV NODE_PATH=/usr/local/lib/node_modules

# The orchestration protocol, as GLOBAL context (~/.pi/agent/AGENTS.md).
# Pi loads AGENTS.md from the working directory, its parents, and this path.
# It lives here rather than in /workspace because projects are cloned into
# subdirectories, and a protocol that only applies at the workspace root would
# be invisible from inside a clone.
COPY sandbox-AGENTS.md /root/.pi/agent/AGENTS.md

WORKDIR /workspace

# Default to launching the Pi TUI. The runner overrides the command with
# ["pi", "-p", "<task>"] for one-shot runs.
CMD ["pi"]
