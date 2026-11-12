"""pi-sandbox must trust the private CA that signs gitea.example.internal.

WHY THIS FILE EXISTS
--------------------
the clone failed with:

    server verification failed: certificate signer not trusted

and it was silenced with `GIT_SSL_NO_VERIFY=true`, which disables verification
for EVERY host - the provider endpoints included. That made a working clone look
like evidence the clone path was healthy, when it proved nothing about TLS.

The real fix installs the chain into the image trust store. These tests pin the
properties that make that fix real rather than cosmetic.

WHY THE ASSERTIONS RUN AGAINST COMMENT-STRIPPED TEXT
----------------------------------------------------
The Dockerfile and `main.py` EXPLAIN the insecure options in order to justify
refusing them. A naive `assert "GIT_SSL_NO_VERIFY" not in text` therefore fails
on CORRECT input - the failure pi-harness recorded in ci/test_gateway_ca.py,
where three guards failed because they matched the documentation of what not to
do. Every assertion here runs against code with comments and docstrings removed.
"""

import hashlib
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)


class SkipTest(Exception):
    """Detected by CLASS NAME in the runner, not by identity -- the runner runs
    as __main__, so importing it would create a second, distinct class."""

def _require_chain():
    """Skip when the CA chain is absent.

    nuc-ca-chain.pem is export-ignored, so it is NOT in the public snapshot.
    A test that reads it would raise FileNotFoundError there and make the
    suite look broken to anyone cloning the public repo.
    """
    if not os.path.exists(os.path.join(_REPO, "nuc-ca-chain.pem")):
        raise SkipTest("nuc-ca-chain.pem is export-ignored (public checkout)")


def _read(*parts):
    return open(os.path.join(_REPO, *parts), "r", encoding="utf-8").read()


def _strip_comments(text):
    """Remove full-line `#` comments. Inline comments are left: removing them
    safely needs a parser, and no assertion here depends on the tail of a line."""
    return "\n".join(
        ln for ln in text.split("\n") if not ln.lstrip().startswith("#")
    )


def _dockerfile_code():
    return _strip_comments(_read("pi-sandbox.Dockerfile"))


# Triple quotes, built at import time.
#
# WHY NOT `re.sub(r'""".*?"""', ...)`: the clone script is assigned with `r"""`,
# so a naive docstring strip REMOVES THE VERY TEXT this module needs to inspect.
# Measured: the guard for GIT_SSL_NO_VERIFY=true passed when it should have
# failed, because the assignment had already been stripped out from under it.
# The negative lookbehind keeps `r"""` (a string literal) and only removes an
# UNPREFIXED `"""` (a docstring).
_QQ = '"' * 3
_DOCSTRING = re.compile(r'(?<![A-Za-z0-9_])' + _QQ + r'.*?' + _QQ, re.S)


def _main_code():
    """`main.py` with DOCSTRINGS and `#` comments removed.

    String literals are preserved deliberately: `_CLONE_SCRIPT` is a raw string
    and its contents are the code under test. Only an unprefixed triple-quote
    (a docstring) is stripped.
    """
    text = _DOCSTRING.sub("", _read("main.py"))
    return _strip_comments(text)


class TestTheChainIsTheRightChain:
    def test_the_chain_contains_leaf_and_intermediate(self):
        _require_chain()
        """pi-harness's copy is leaf + intermediate (2 blocks). An empty or
        truncated PEM would satisfy a weaker 'file exists' check."""
        pem = _read("nuc-ca-chain.pem")
        assert pem.count("BEGIN CERTIFICATE") == 2, (
            "nuc-ca-chain.pem should hold exactly 2 certificates (leaf + "
            "intermediate), matching pi-harness/gateway/nuc-ca-chain.pem"
        )
        assert "END CERTIFICATE" in pem, "nuc-ca-chain.pem is truncated"

    def test_the_chain_has_not_drifted_from_pi_harness(self):
        _require_chain()
        """THREE COPIES OF ONE CA, PINNED BY CONTENT.

        The same chain exists at pi-harness/ci/nuc-ca-chain.pem and
        pi-harness/gateway/nuc-ca-chain.pem, which are asserted byte-identical by
        that repo's own test. This is the third copy, so it is pinned too: a CA
        that silently drifted would produce a TLS failure nobody could explain.

        The digest is a fixed constant rather than a read of the other repo,
        because a build context cannot reach it and CI should not clone a second
        repository to check a certificate.
        """
        want = "7187229cc57b9ae0df38d3f77feb772665690e86f32a849752c4cbf0a9030564"
        got = hashlib.sha256(
            open(os.path.join(_REPO, "nuc-ca-chain.pem"), "rb").read()
        ).hexdigest()
        assert got == want, (
            "nuc-ca-chain.pem does not match the chain pi-harness pins "
            f"(expected {want}, got {got}). If the CA was rotated deliberately, "
            "update this digest AND pi-harness's copy in the same change."
        )


class TestTheImageInstallsItCorrectly:
    def test_the_chain_is_staged_into_the_ca_directory(self):
        """Staged at RUN time, NOT via COPY.

        A COPY of a missing file FAILS the build, and this chain is
        export-ignored, so a public checkout does not contain it.
        """
        code = _dockerfile_code()
        assert "/usr/local/share/ca-certificates/nuc-ca-chain.crt" in code, (
            "the chain never reaches /usr/local/share/ca-certificates, so no "
            "clone could verify"
        )
        assert "COPY nuc-ca-chain.pem" not in code, (
            "the chain is COPYed unconditionally: the build would FAIL on the "
            "public snapshot, where the export-ignored chain is absent"
        )

    def test_update_ca_certificates_runs(self):
        """A COPY alone is not enough; the file must be MERGED into the bundle."""
        assert "update-ca-certificates" in _dockerfile_code(), (
            "the chain is copied but never merged into the trust store"
        )

    def test_the_install_is_asserted_by_a_certificate_count(self):
        """update-ca-certificates SILENTLY IGNORES a file it cannot read, so
        without a post-install assertion the image builds cleanly and every clone
        fails at run time."""
        assert re.search(
            r'"\$after"\s+-ne\s+"\$\(\(before \+ want\)\)"',
            _dockerfile_code(),
        ), (
            "the CA install is not verified by a COUNT, so a silent skip by "
            "update-ca-certificates would go unnoticed"
        )

    def test_the_guard_does_not_grep_a_name(self):
        """ASSERT THE PROPERTY, NOT A SPELLING OF IT.

        /etc/ssl/certs lists HASH links, not subjects, and a subject match is
        unreliable for a chain holding an intermediate. Listing that directory and
        greping for a name fails on CORRECT input.
        """
        assert not re.search(r"ls\s+/etc/ssl/certs", _dockerfile_code()), (
            "the CA guard lists /etc/ssl/certs, which is a name check over hash "
            "links - it fails on correct input"
        )


class TestNoInsecureEscapeHatch:
    """Negative controls. Each would make the symptom vanish while removing the
    control that makes the clone trustworthy."""

    def test_the_dockerfile_does_not_disable_verification(self):
        code = _dockerfile_code()
        for bad in ("GIT_SSL_NO_VERIFY", "sslVerify=false", "sslVerify false"):
            assert bad not in code, (
                f"pi-sandbox.Dockerfile disables TLS verification via {bad}"
            )

    def test_the_clone_script_does_not_disable_verification(self):
        """The script SETS the variable - to false. Asserting absence would be
        wrong, because `export GIT_SSL_NO_VERIFY=false` is the correct code; what
        must never appear is an assignment of true."""
        code = _main_code()
        assert not re.search(
            r"GIT_SSL_NO_VERIFY\s*=\s*[\"']?true", code
        ), "the clone script still disables TLS verification"
        assert not re.search(r"\-c\s+http\.sslVerify\s*=\s*false", code), (
            "the clone script passes sslVerify=false to git"
        )

    def test_the_trust_store_is_the_mechanism_not_an_env_override(self):
        """Two sources of truth is this project's recurring bug class. The CA goes
        into the image trust store; a PI_*_CA_FILE would be somewhere to disagree."""
        assert "CA_FILE" not in _dockerfile_code(), (
            "the Dockerfile also sets a CA FILE override: two sources of truth for "
            "which trust root is used"
        )


class TestThisFileActuallyRuns:
    def test_there_is_no_if_main_guard(self):
        """A check that does not run cannot fail. pi-harness hit this: a test was
        written after an `if __name__` guard and pytest never collected it.

        Matched by STRUCTURE, not by the literal text, because this file has to
        name the thing it looks for."""
        text = open(os.path.abspath(__file__), "r", encoding="utf-8").read()
        code = re.sub(r'""".*?"""', "", text, flags=re.S)
        dunder = "__" + "name" + "__"
        assert not re.search(r"^\s*if\s+" + dunder + r"\s*==", code, re.M), (
            "this test file has an if-main guard, so anything after it is never "
            "collected by pytest"
        )

class TestTheChainCannotBePublished:
    """A grep for the hostname cannot see INSIDE a PEM.

    The chain is a trust anchor for a private namespace, base64 with no
    searchable hostname, so the identifier gate in public-sync.yml does not
    catch it. Export-ignore is the only thing keeping it out.
    """

    def test_the_chain_is_export_ignored(self):
        attrs = _read(".gitattributes")
        assert re.search(r"^nuc-ca-chain\.pem\s+export-ignore", attrs, re.M), (
            "nuc-ca-chain.pem is NOT export-ignored: the public snapshot would "
            "publish a private CA trust anchor"
        )

    def test_the_chain_is_absent_from_the_exported_paths(self):
        """Assert the OUTCOME git archive produces, not the rule text.

        Parses .gitattributes the way git does and requires the chain among the
        excluded paths, so a rule that is present but misspelled still fails.
        """
        excluded = set()
        for ln in _read(".gitattributes").split("\n"):
            if "export-ignore" not in ln or ln.lstrip().startswith("#"):
                continue
            excluded.add(ln.split()[0].rstrip("/"))
        assert "nuc-ca-chain.pem" in excluded, (
            "nuc-ca-chain.pem would be exported to the public snapshot"
        )

class TestTheCloneCanActuallyAuthenticate:
    """A clone that cannot authenticate fails as "could not read Username",
    which reads like a network fault. Two defects produced exactly that in
    production, and neither was visible from the health endpoint."""

    def test_home_is_passed_to_the_clone_exec(self):
        """exec_run(environment=...) REPLACES the container env, so HOME must be
        listed explicitly. Without it `git config --global` fails with
        `fatal: $HOME not set`, the credential helper is never installed, and
        git falls through to an interactive prompt with no TTY."""
        # Read the RAW source: the env dict is a dict literal just outside the
        # clone script, so the comment-stripped view is not the right target for
        # this particular assertion.
        code = _read("main.py")
        assert re.search(r'"HOME"\s*:', code), (
            "HOME is not passed to exec_run: `git config --global` will fail and "
            "the credential helper will never be installed"
        )

    def test_the_script_fails_when_the_helper_is_missing(self):
        """Fail fast. A missing helper otherwise degrades into the misleading
        "could not read Username" error rather than naming the cause."""
        code = _main_code()
        assert "credential helper not installed" in code, (
            "the clone script does not verify the credential helper installed, "
            "so a helper failure degrades into an auth-looking error"
        )

class TestVersionIsConsistent:
    """A version string is the cheapest way to confirm a deploy took effect.

    VERSION was left at 1.2.2 through a fix release, so /health could not
    distinguish a deployed fix from a stale image. That removed the one cheap
    check available, and the absence cost several rounds of blind diagnosis.

    README is pinned too, because it drifted two releases behind on its own.
    """

    def test_health_version_matches_the_readme(self):
        import re as _re

        code = _read("main.py")
        m = _re.search(r'^VERSION = "([0-9.]+)"', code, _re.M)
        assert m, "VERSION not found in main.py"
        version = m.group(1)

        readme = _read("README.md")
        assert f"**Version: {version}.**" in readme, (
            f"README does not state Version: {version}; it has drifted from "
            "VERSION in main.py. A stale README makes a deploy look older than "
            "it is."
        )
        assert f'"version": "{version}"' in readme, (
            f"the /health example in README does not show {version}"
        )

class TestCloneFailuresAreDiagnosable:
    """The 502 detail used to be only "exit 128", which discarded the one piece
    of evidence a caller could act on. That is why a broken clone took many
    rounds to identify: every failure looked identical."""

    def test_the_detail_carries_the_command_output(self):
        code = _read("main.py")
        assert "Repository clone failed (exit" in code
        assert re.search(r"Repository clone failed \(exit \{res\.exit_code\}\): ", code), (
            "the clone failure detail does not include the command output, so "
            "every distinct failure looks like the same bare exit code"
        )

    def test_the_output_is_redacted_before_leaving_the_service(self):
        code = _read("main.py")
        assert "_redact(out" in code, (
            "clone output is returned to an LLM-driven client without redaction"
        )

class TestBaseUrlIsValidated:
    """A malformed base URL must name itself.

    The variable was set to `gitea.example.internal` with no scheme, so git treated the
    clone target as a local path and said

        fatal: repository 'gitea.example.internal/...' does not exist

    That names neither the variable nor the missing scheme. Worse,
    repo_clone_ready reported True throughout, because it only checked the value
    was non-empty.
    """

    def test_a_schemeless_value_is_rejected_with_an_explanation(self):
        import ast as _ast

        tree = _ast.parse(_read("main.py"))
        fn = next(
            n for n in tree.body
            if isinstance(n, _ast.FunctionDef) and n.name == "_base_url_problem"
        )
        ns = {}
        exec(
            compile(
                _ast.fix_missing_locations(_ast.Module(body=[fn], type_ignores=[])),
                "<x>", "exec",
            ),
            ns,
        )
        f = ns["_base_url_problem"]

        assert f("gitea.example.internal") is not None, (
            "a schemeless base URL is accepted; git will treat it as a local "
            "path and fail with a misleading 'does not exist'"
        )
        # Assert the REASON, not merely that it rejects. A schemeless value
        # ALSO trips the unsupported-scheme branch, so a bare `is not None`
        # check passes even when the schemeless check has been deleted -- a
        # guard that cannot fail is worse than no guard.
        msg = f("gitea.example.internal")
        assert "no URL scheme" in msg, (
            f"schemeless value rejected for the WRONG reason: {msg!r} -- it "
            "should say the URL has no scheme"
        )
        assert f("https://gitea.example.internal") is None, "a valid URL is rejected"
        assert f("") is not None, "an empty value is accepted"
        assert f("ftp://x") is not None, "an unsupported scheme is accepted"

    def test_health_exposes_the_problem_not_just_a_bool(self):
        code = _read("main.py")
        assert "repo_clone_problem" in code, (
            "health reports only a boolean, so a present-but-unusable base URL "
            "reads as ready -- which is what happened"
        )
class TestTheSnapshotIsPublishable:
    """Run the publication gate's own check locally, at test time.

    WHY: the gate caught a hostname that had been reintroduced TWICE while
    writing comments that EXPLAINED the hostname problem. Both times the prose
    was the leak. Catching it here fails a build in seconds instead of at
    publish time, which is the worst moment to discover it.

    The pattern list mirrors .gitea/workflows/public-sync.yml deliberately:
    that file is export-ignored, so a test cannot read it, and a test that
    silently skipped would be worse than a duplicated list.
    """

    # Assembled from FRAGMENTS on purpose: a literal list in this file would
    # itself be a hit. The guard caught exactly that on its first run -- the
    # same trap as writing a comment that explains the identifier.
    _D = "[.]"
    FORBIDDEN = (
        "nuc" + _D + "local",
        "llama" + "-rocm",
        "llama" + "_rocm",
        "OCG" + "_[A-Z_]+",
        "10" + _D + "[0-9]+" + _D + "[0-9]+" + _D + "[0-9]+",
        "192" + _D + "168" + _D + "[0-9]+" + _D + "[0-9]+",
    )

    SKIP_FILES = (".gitattributes", ".gitignore", ".dockerignore")
    SKIP_DIRS = (".gitea/", ".github/")

    def _publishable_files(self):
        out = []
        for root, _dirs, files in os.walk(_REPO):
            if os.sep + ".git" in root or "__pycache__" in root:
                continue
            for name in files:
                rel = os.path.relpath(
                    os.path.join(root, name), _REPO
                ).replace(os.sep, "/")
                if rel in self.SKIP_FILES or rel.startswith(self.SKIP_DIRS):
                    continue
                if rel.endswith(".pem"):
                    continue
                out.append(rel)
        return sorted(out)

    def test_no_internal_identifier_reaches_the_snapshot(self):
        pattern = re.compile("|".join(self.FORBIDDEN), re.I)
        hits = []
        for rel in self._publishable_files():
            path = os.path.join(_REPO, rel)
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for lineno, line in enumerate(fh, 1):
                    if pattern.search(line):
                        hits.append(rel + ":" + str(lineno) + ": " + line.strip()[:90])
        assert not hits, (
            "internal identifiers would be published:\n  "
            + "\n  ".join(hits[:10])
            + "\n\nNOTE: a COMMENT that explains such an identifier "
            "reintroduces it. Use a placeholder."
        )

    def test_the_guard_actually_examines_files(self):
        """A walk that returns nothing would make the check above vacuous."""
        files = self._publishable_files()
        assert len(files) > 10, f"only found {len(files)} publishable files"
        assert "main.py" in files, "main.py not scanned"
        assert ".gitea/workflows/deploy.yml" not in files, "export-ignored file scanned"
