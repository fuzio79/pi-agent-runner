"""Run the static guards with NO third-party dependencies.

WHY THIS EXISTS
---------------
The guards were run with `python3 -m pytest`, which failed on CI with

    /var/run/act/workflow/1.sh: line 9: python3: not found

The job container is `docker:24` (Alpine), which ships no python3, and adding
pip + pytest to it means a network install and PEP 668 handling for tests that
use nothing but the standard library.

So the guards stay pytest-compatible (pytest still collects them locally) and
this file runs them with plain python3. It collects top-level `test_*`
functions and `test_*` methods on `Test*` classes -- the shape these tests use --
and exits non-zero on any failure.

WHAT IT IS NOT: a pytest replacement. No fixtures, no parametrisation, no
setup/teardown. If a guard ever needs those, use pytest and add python3 to the
job container instead.
"""

import importlib.util
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))


class SkipTest(Exception):
    """Raised by a test that cannot run in this checkout, e.g. because the
    file it reads is export-ignored and so absent from the PUBLIC
    snapshot. Reported as a SKIP, never as a pass: a skip counted as a
    pass would be a guard that protects nothing."""


def _load(path):
    name = os.path.splitext(os.path.basename(path))[0]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so intra-test imports work.
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _collect(mod):
    """Yield (label, callable) for module-level test_* and Test*.test_*."""
    for attr in sorted(dir(mod)):
        obj = getattr(mod, attr)
        if attr.startswith("test_") and callable(obj):
            yield ("%s::%s" % (mod.__name__, attr), obj)
        elif attr.startswith("Test") and isinstance(obj, type):
            inst = obj()
            for meth in sorted(dir(inst)):
                if meth.startswith("test_"):
                    yield (
                        "%s::%s::%s" % (mod.__name__, attr, meth),
                        getattr(inst, meth),
                    )


def main(argv):
    files = argv[1:]
    if not files:
        files = [
            os.path.join(_HERE, f)
            for f in sorted(os.listdir(_HERE))
            if f.startswith("test_") and f.endswith(".py")
        ]
    passed = failed = skipped = 0
    failures = []
    for path in files:
        try:
            mod = _load(path)
        except Exception:
            failed += 1
            failures.append((os.path.basename(path), traceback.format_exc()))
            continue
        for label, fn in _collect(mod):
            try:
                fn()
                passed += 1
                print("ok    %s" % label)
            except Exception as _skip_probe:
                if type(_skip_probe).__name__ != "SkipTest":
                    raise
                skipped += 1
                print("skip  %s (%s)" % (label, _skip_probe))
            except AssertionError as e:
                failed += 1
                failures.append((label, str(e) or "assertion failed"))
                print("FAIL  %s" % label)
            except Exception:
                failed += 1
                failures.append((label, traceback.format_exc()))
                print("ERROR %s" % label)

    summary = "\n%d passed, %d failed" % (passed, failed)
    if skipped:
        summary += ", %d skipped" % skipped
    print(summary)
    if failures:
        print("\n--- failures ---")
        for label, detail in failures:
            print("\n%s\n%s" % (label, detail))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
