#!/bin/sh
# Resolve relative paths from the project, not the caller's working directory.
set -eu
CZZ_ROOT=$(CDPATH= cd -P -- "$(dirname -- "$0")" && pwd)
cd "$CZZ_ROOT"
CZZ_VENV="$CZZ_ROOT/.venv/bin/python"
if [ ! -x "$CZZ_VENV" ] && [ -f "$CZZ_ROOT/.venv/Scripts/python.exe" ]; then
  CZZ_VENV="$CZZ_ROOT/.venv/Scripts/python.exe"
fi
if ! "$CZZ_VENV" -E -s -c 'import sys; from pathlib import Path; assert sys.version_info >= (3,10); assert sys.prefix != sys.base_prefix; assert Path(sys.prefix).resolve() == Path(".venv").resolve(); assert "include-system-site-packages = false" in Path(".venv/pyvenv.cfg").read_text().lower()' >/dev/null 2>&1; then
  printf '%s\n' 'Run install.sh first to prepare Python for this project.' >&2
  exit 1
fi
# With no arguments, CzzEdit opens its local browser UI. Ctrl+C stops it.
# Examples: sh run.sh serve --port 8766 / sh run.sh demo
exec "$CZZ_VENV" -E -s -m czzedit "$@"
