#!/bin/sh
# macOS/Linux: reuse Python >=3.10, otherwise install a project-local runtime.
# uv installer reference: https://docs.astral.sh/uv/reference/installer/
set -eu
CZZ_ROOT=$(CDPATH= cd -P -- "$(dirname -- "$0")" && pwd)
cd "$CZZ_ROOT"
CZZ_LOGIN=0
case "${1-}" in
  '') ;;
  --login) CZZ_LOGIN=1 ;;
  -h|--help) printf '%s\n' 'Usage: sh install.sh [--login]' 'Python 3.10+ is reused; otherwise Python 3.13 is installed locally.'; exit 0 ;;
  *) printf '%s\n' 'Usage: sh install.sh [--login]' >&2; exit 2 ;;
esac
[ "$#" -le 1 ] || { printf '%s\n' 'Too many arguments.' >&2; exit 2; }
[ -f czzedit/__main__.py ] || { printf '%s\n' 'Keep this script in the project folder.' >&2; exit 1; }
unset PYTHONHOME PYTHONPATH
export PYTHONNOUSERSITE=1
CZZ_RUNTIME="$CZZ_ROOT/.czzedit/runtime"
export UV_UNMANAGED_INSTALL="$CZZ_RUNTIME/uv"
export UV_PYTHON_INSTALL_DIR="$CZZ_RUNTIME/python"
export UV_CACHE_DIR="$CZZ_RUNTIME/cache"
export UV_PYTHON_INSTALL_BIN=0 UV_PYTHON_INSTALL_REGISTRY=0 UV_NO_CONFIG=1
CZZ_UV="$UV_UNMANAGED_INSTALL/uv"
CZZ_VENV="$CZZ_ROOT/.venv/bin/python"
[ ! -f "$CZZ_ROOT/.venv/Scripts/python.exe" ] || CZZ_VENV="$CZZ_ROOT/.venv/Scripts/python.exe"
CZZ_TEMP=''
trap '[ -z "$CZZ_TEMP" ] || rm -f "$CZZ_TEMP"' EXIT
ensure_uv() {
  if [ -x "$CZZ_UV" ]; then return; fi
  mkdir -p "$CZZ_RUNTIME"
  CZZ_TEMP=$(mktemp "$CZZ_RUNTIME/uv-installer.XXXXXX")
  printf '%s\n' 'Downloading the project-local Python installer (uv)...'
  if command -v curl >/dev/null 2>&1; then
    curl --proto '=https' --tlsv1.2 -fLsS --retry 3 https://astral.sh/uv/install.sh -o "$CZZ_TEMP"
  elif command -v wget >/dev/null 2>&1; then
    wget -q https://astral.sh/uv/install.sh -O "$CZZ_TEMP"
  else
    printf '%s\n' 'curl or wget is required to download Python.' >&2; exit 1
  fi
  sh "$CZZ_TEMP"
  [ -x "$CZZ_UV" ] || { printf '%s\n' 'uv installation failed.' >&2; exit 1; }
}
if [ ! -e .venv ]; then
  CZZ_PY=''
  for CZZ_CANDIDATE in python3 python python3.15 python3.14 python3.13 python3.12 python3.11 python3.10; do
    if command -v "$CZZ_CANDIDATE" >/dev/null 2>&1 &&
       "$CZZ_CANDIDATE" -E -s -c 'import sys, sqlite3; assert sys.version_info >= (3,10)' >/dev/null 2>&1; then
      CZZ_PY=$("$CZZ_CANDIDATE" -E -s -c 'import sys; print(sys.executable)'); break
    fi
  done
  if [ -n "$CZZ_PY" ] && "$CZZ_PY" -E -s -c 'import venv' >/dev/null 2>&1; then
    printf 'Reusing Python: %s\n' "$CZZ_PY"
    # Dependencies are installed into this environment below, using uv.
    "$CZZ_PY" -E -s -m venv --without-pip .venv
  else
    ensure_uv
    if [ -z "$CZZ_PY" ]; then
      CZZ_PY=$("$CZZ_UV" python find --system --no-managed-python --no-python-downloads '>=3.10' 2>/dev/null) || CZZ_PY=''
    fi
    if [ -z "$CZZ_PY" ]; then
      printf '%s\n' 'No compatible Python found. Installing Python 3.13 for this OS/architecture...'
      "$CZZ_UV" python install 3.13 --no-bin
      CZZ_PY=$("$CZZ_UV" python find --system --managed-python --no-python-downloads 3.13)
    else
      printf 'Reusing Python: %s\n' "$CZZ_PY"
    fi
    "$CZZ_UV" venv --python "$CZZ_PY" --no-python-downloads .venv
  fi
fi
if ! "$CZZ_VENV" -E -s -c 'import sys, sqlite3; from pathlib import Path; assert sys.version_info >= (3,10); assert Path(sys.prefix).resolve() == Path(".venv").resolve(); assert sys.prefix != sys.base_prefix; assert "include-system-site-packages = false" in Path(".venv/pyvenv.cfg").read_text().lower()' >/dev/null 2>&1; then
  printf '%s\n' 'Existing .venv is incompatible, incomplete, or allows system packages. Rename it and rerun install.sh. Saved chats are kept separately in .czzedit.' >&2
  exit 1
fi
ensure_uv
"$CZZ_UV" pip install --python "$CZZ_VENV" -r requirements.txt
if [ "$CZZ_LOGIN" -eq 1 ]; then
  ensure_uv
  "$CZZ_UV" pip install --python "$CZZ_VENV" -r requirements-login.txt
  "$CZZ_VENV" -E -s -m playwright install chromium
fi
"$CZZ_VENV" -E -s -m czzedit --help >/dev/null
"$CZZ_VENV" -E -s -c 'import sys; print("Ready: Python " + sys.version.split()[0] + " in " + sys.prefix)'
printf '%s\n' 'Start CzzEdit with: sh run.sh'
