#!/usr/bin/env python3
"""Hold the modules core holds to full coverage to the same bar.

Reads the coverage.py JSON report pytest-cov wrote for the consumer's suite and fails
when any line of a config flow or of diagnostics did not run. Exit 1 on any failure.
README.md says why these modules and how python-validate.yml runs it.
"""

import argparse
import json
import pathlib
import sys

# The modules core's codecov.yml holds to a 100% target.
FULL = ("config_flow.py", "diagnostics.py")


def _key(path: str, root: pathlib.Path) -> str:
    """A report path as a repo-relative POSIX path, however pytest-cov wrote it."""
    p = pathlib.Path(path)
    if p.is_absolute():
        try:
            p = p.relative_to(root.resolve())
        except ValueError:
            return p.as_posix()
    return p.as_posix()


def problems(root: pathlib.Path, report: dict) -> list[str]:
    """Every module held to full coverage with a line that did not run."""
    files = {_key(k, root): v for k, v in (report.get("files") or {}).items()}
    found = []
    for module in sorted(root.glob("custom_components/*/*.py")):
        if module.name not in FULL:
            continue
        rel = module.relative_to(root).as_posix()
        if rel not in files:
            found.append(f"{rel} never ran under the tests (0% covered)")
            continue
        missing = files[rel].get("missing_lines") or []
        if missing:
            found.append(
                f"{rel} is not fully covered; lines that never ran: "
                + ", ".join(str(n) for n in missing)
            )
    return found


def main(argv: list[str] | None = None) -> int:
    """Check the report against --root; exit 1 on any module below the bar."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=".", help="repository the report covers")
    ap.add_argument("--report", default="coverage.json", help="coverage.py JSON report")
    args = ap.parse_args(argv)

    root = pathlib.Path(args.root)
    report = json.loads(pathlib.Path(args.report).read_text(encoding="utf-8"))
    found = problems(root, report)
    for f in found:
        print(f"❌ FAIL: {f}")
    if not found:
        print("✅ config flow and diagnostics fully covered")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
