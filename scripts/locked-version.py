#!/usr/bin/env python3
"""Print the version uv.lock pins for a package: `scripts/locked-version.py duckdb`.

For CI, which installs duckdb outside `uv sync` (pyproject [tool.uv]
no-install-package) and must still take its version from the lockfile."""

import sys
import tomllib
from pathlib import Path

lock = tomllib.loads((Path(__file__).resolve().parent.parent / "uv.lock").read_text())
name = sys.argv[1]
versions = {p["version"] for p in lock["package"] if p["name"] == name}
if len(versions) != 1:
    sys.exit(f"locked-version: {name}: expected one locked version, found {sorted(versions)}")
print(versions.pop())
