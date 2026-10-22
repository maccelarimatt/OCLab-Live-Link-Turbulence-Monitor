#!/bin/sh
# Install with the Python interpreter used by the normal Linux terminal.
set -eu

oclab_repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# Editors distributed as Flatpaks may have a different /usr/bin/python3 from
# the host. A venv made there can break when launched outside the editor.
if [ -f /.flatpak-info ]; then
    if ! command -v flatpak-spawn >/dev/null 2>&1; then
        echo "Run ./install.sh from your normal Linux terminal (outside Flatpak)." >&2
        exit 1
    fi
    echo "Installing with the host Python, outside the editor's Flatpak runtime."
    exec flatpak-spawn --host sh "$oclab_repo_dir/install.sh" "$@"
fi

cd "$oclab_repo_dir"
# Override when the default python3 is older than 3.12, e.g. PYTHON=python3.12 ./install.sh
oclab_python=${PYTHON:-python3}
"$oclab_python" - <<'PY'
import sys
if sys.version_info < (3, 12):
    raise SystemExit("Python 3.12 or newer is required. Install it, then run: PYTHON=python3.12 ./install.sh")
print(f"Using {sys.executable} (Python {sys.version.split()[0]})", flush=True)
PY

if [ -e .venv ]; then
    if ! "$oclab_python" - <<'PY'
from pathlib import Path
import subprocess
import sys

venv = Path('.venv').resolve()
try:
    config = dict(line.split(' = ', 1) for line in (venv / 'pyvenv.cfg').read_text().splitlines() if ' = ' in line)
    if config.get('version', '').split('.')[:2] != [str(n) for n in sys.version_info[:2]]:
        raise ValueError('Python version changed')
    subprocess.run([str(venv / 'bin/python'), '-c',
                    'import pip, sys; from pathlib import Path; '
                    'assert Path(sys.prefix).resolve() == Path(sys.argv[1]).resolve()', str(venv)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
except (OSError, ValueError, subprocess.SubprocessError):
    sys.exit(1)
PY
    then
        oclab_backup=".venv.backup-$(date +%Y%m%d-%H%M%S)-$$"
        mv .venv "$oclab_backup"
        echo "Preserved incompatible environment at $oclab_backup"
    fi
fi

if [ ! -d .venv ]; then
    "$oclab_python" -m venv .venv
fi

.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/python -c 'from oclab.__main__ import main; import oclab; print("Installed:", oclab.__file__)'
echo "Ready. From your normal Linux terminal, start a demo with:"
echo "  .venv/bin/oclab --mode demo --data-dir ./data/demo"
