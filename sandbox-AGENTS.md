# Working protocol for agent runs in this sandbox

This file is baked into the image at `~/.pi/agent/AGENTS.md`, so it is global
context for every run. It describes how to work in a repository under
`/workspace`. It does not describe any particular project; a project's own
`AGENTS.md` takes precedence over this one, and you should read it first.

## Where code lives

If the task named a repository, it has already been cloned into
`/workspace/<name>/` **before** you started -- it is there when your first tool
call runs, and you do not need to clone it and should not try. Work inside that
directory. `/workspace` itself is not a repository.

If a repository was requested but `/workspace` is empty, say so plainly instead
of inventing files. That means the clone step failed and the caller needs to
know, not be given a guess.


Repositories are cloned into `/workspace/<repo-name>/`. `/workspace` itself is
not a repository. Work inside the clone, not beside it.

## Never invent the project's checks

If the repository has `AGENTS.md`, `CHECKS.md`, a `Makefile`, or a manifest
(`package.json` scripts, `pyproject.toml`, `tox.ini`, `go.mod`, `Cargo.toml`),
those define how the project is verified. Run **those** commands. Do not
substitute a runner you prefer, and do not add flags the project does not use.

If none exists, say so plainly instead of inventing a suite.

## Python projects: use uv, not the system python

The system interpreter is Debian's `python3` (3.11) and there is no `pip`. Do
not try to `pip install` against it, and do not conclude a project is
unverifiable because its `requires-python` is newer: `uv` is installed and can
fetch any CPython version into an isolated environment.

```
uv venv --python 3.12 .venv        # honours requires-python
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/pytest -q
```

Rules that follow from this:

- If the project pins a version, use **its** version, not the system one.
- A `make` target that calls `ruff`, `mypy` or `pytest` needs that venv on PATH:
  `PATH="$PWD/.venv/bin:$PATH" make check`. Do not edit the Makefile to make it
  work — run it as the project intends.
- Installing into a venv is expected and cheap. Installing into the system
  interpreter is not, and nothing you install survives the run anyway.

## Delegation

You have the `subagent` tool. Use it when a task genuinely benefits from a
separate context window — a review of finished work, a second opinion on a
risky decision, or running the project's checks — not for work you can finish
yourself in a few tool calls.

Available roles:

| role         | can edit source | purpose                                        |
| ------------ | --------------- | ---------------------------------------------- |
| `scout`      | no              | Fast recon: relevant files, entry points, risk |
| `reviewer`   | no              | Judgment review; cannot run commands           |
| `verifier`   | no              | Runs the project's checks and pastes output    |
| `programmer` | yes             | Executes an approved plan, self-verifies       |
| `oracle`     | no              | Expensive second opinion (costly model)        |

Two distinctions that matter:

- `reviewer` has **no shell**, so it reasons about whether a test *should*
  change but cannot tell you whether it *passes*. `verifier` has `bash` and no
  `edit`/`write`, so it can run checks but cannot mutate source. Use both when
  you need both answers; do not ask `reviewer` to run anything.
- `oracle` runs on a different, more expensive model. Invoke it deliberately
  for a final opinion on something large or risky, never as a routine step.

Delegation is not free: each child is a full agent loop. One well-scoped
review is worth more than three speculative ones.

## Reporting

State what you actually ran and what it actually returned. If a check failed,
paste the output. If you did not run a check, say that instead of implying it
passed. "I did not verify this" is an acceptable answer; a confident claim that
turns out to be false is not.

## Boundaries

You are a disposable container. Do not attempt to reach or modify anything
outside `/workspace` except through your own tools' normal operation. Never
commit or push unless the task explicitly asks for it, and never to a branch
other than the one you were given.
