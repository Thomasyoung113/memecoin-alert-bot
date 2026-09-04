"""Test runner shim: uses pytest if available, else the built-in runner.

The suite in test_money_paths.py is written so every test is a plain
function/class usable by both. This shim collects `Test*` classes and
`test_*` functions (pytest-style, no fixtures) and runs them with
assert-based pass/fail.
"""
import sys
import traceback
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def run_without_pytest(module):
    passed, failed = [], []
    for name in dir(module):
        if not name.startswith("Test"):
            continue
        cls = getattr(module, name)
        if not isinstance(cls, type):
            continue
        inst = cls()
        for mname in sorted(dir(inst)):
            if not mname.startswith("test_"):
                continue
            full = f"{name}::{mname}"
            try:
                getattr(inst, mname)()
                passed.append(full)
            except Exception:
                failed.append((full, traceback.format_exc(limit=3)))
    for name in dir(module):
        if name.startswith("test_") and callable(getattr(module, name)):
            try:
                getattr(module, name)()
                passed.append(name)
            except Exception:
                failed.append((name, traceback.format_exc(limit=3)))
    return passed, failed


if __name__ == "__main__":
    import test_money_paths as suite

    try:
        import pytest  # noqa
        raise SystemExit(pytest.main([os.path.join(os.path.dirname(
            os.path.abspath(__file__)), "test_money_paths.py"), "-v"]))
    except ImportError:
        passed, failed = run_without_pytest(suite)
        print(f"\nPASSED: {len(passed)}  FAILED: {len(failed)}")
        for full in passed:
            print(f"  ✅ {full}")
        for full, tb in failed:
            print(f"  ❌ {full}")
            print("     " + tb.replace("\n", "\n     ").strip()[-400:])
        raise SystemExit(1 if failed else 0)
