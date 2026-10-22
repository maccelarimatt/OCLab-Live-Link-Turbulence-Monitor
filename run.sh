#!/usr/bin/env bash
# One command to set up and start the OC Lab live-link monitor on this PC.
# Safe to run every time: each step only acts when something is missing.
#
#   ./run.sh                 set up if needed, then record (hardware mode)
#   ./run.sh --demo          synthetic data in ./data/demo
#   ./run.sh --flash         (re)install the Pico weather firmware first
#   ./run.sh --setup-only    set up and check devices, do not start
#   ./run.sh --yes           answer yes to prompts (unattended use)
# Other options pass through to oclab, e.g. ./run.sh --host 0.0.0.0
set -euo pipefail

repo=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$repo"

demo=0 flash=0 setup_only=0 yes=0 passthrough=()
for arg in "$@"; do
    case "$arg" in
        --demo) demo=1 ;;
        --flash) flash=1 ;;
        --setup-only) setup_only=1 ;;
        --yes|-y) yes=1 ;;
        -h|--help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) passthrough+=("$arg") ;;
    esac
done

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
note() { printf '   %s\n' "$*"; }
warn() { printf '   \033[33mWARNING:\033[0m %s\n' "$*"; }

confirm() {  # confirm "question" -> 0 for yes
    [ "$yes" = 1 ] && return 0
    [ -t 0 ] || return 1
    read -r -p "   $1 [Y/n] " answer
    case "${answer:-y}" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

# Run a list of root commands, after showing them. Without a terminal and
# without cached sudo, print them for the user instead of hanging.
as_root() {
    local reason=$1; shift
    note "Needs sudo to $reason:"
    local cmd
    for cmd in "$@"; do note "  sudo $cmd"; done
    if [ "$(id -u)" = 0 ]; then
        for cmd in "$@"; do eval "$cmd"; done
    elif [ -t 0 ] || sudo -n true 2>/dev/null; then
        for cmd in "$@"; do eval "sudo $cmd"; done
    else
        warn "No terminal for the sudo password; run the commands above yourself."
        return 1
    fi
}

# 1. Python 3.12+ ------------------------------------------------------------
step "Python"
python=""
for candidate in python3.13 python3.12 python3; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; then
        python=$candidate; break
    fi
done
if [ -z "$python" ]; then
    note "Python 3.12 or newer is required; this system has $(python3 --version 2>&1)."
    if command -v apt-get >/dev/null 2>&1 && confirm "Install python3.12 from the deadsnakes PPA?"; then
        as_root "install Python 3.12" \
            "apt-get install -y software-properties-common" \
            "add-apt-repository -y ppa:deadsnakes/ppa" \
            "apt-get update" \
            "apt-get install -y python3.12 python3.12-venv python3.12-dev libusb-1.0-0"
        python=python3.12
    else
        echo "Install Python 3.12+ and run ./run.sh again." >&2; exit 1
    fi
fi
note "Using $python ($("$python" --version 2>&1))"
if ! "$python" -c 'import ensurepip' 2>/dev/null; then
    as_root "add venv support for $python" "apt-get install -y ${python}-venv" ||
        { echo "Install ${python}-venv and run ./run.sh again." >&2; exit 1; }
fi

# 2. Virtual environment -----------------------------------------------------------
step "Application environment"
stamp=.venv/.oclab-installed
wanted=$(cat requirements.lock pyproject.toml | sha256sum | cut -d' ' -f1)
if [ -x .venv/bin/oclab ] && [ "$(cat "$stamp" 2>/dev/null)" = "$wanted" ] &&
   .venv/bin/python -c 'import oclab, serial' 2>/dev/null; then
    note "Up to date (.venv)"
else
    note "Installing or repairing .venv (this can take a minute)"
    PYTHON=$python sh ./install.sh
    echo "$wanted" > "$stamp"
fi

# 3. Configuration -----------------------------------------------------------
step "Configuration"
config=config.local.toml
if [ ! -f "$config" ]; then
    cp config.example.toml "$config"
    note "Created $config from config.example.toml; edit it for calibration and contact details."
else
    note "Using $config"
fi

# 4. Device access (scope over libusb, Pico over /dev/ttyACM*) -------------------------
step "Device access"
root_cmds=() reasons=()
for rule in 70-oclab-hantek.rules 71-oclab-pico-weather.rules; do
    if ! cmp -s "deploy/$rule" "/etc/udev/rules.d/$rule"; then
        root_cmds+=("install -m 0644 '$repo/deploy/$rule' /etc/udev/rules.d/$rule")
        reasons+=("udev rule $rule")
    fi
done
if [ ${#root_cmds[@]} -gt 0 ]; then
    root_cmds+=("udevadm control --reload-rules"
                "udevadm trigger --subsystem-match=usb --subsystem-match=tty")
fi
getent group oclab >/dev/null || { root_cmds=("groupadd -f oclab" "${root_cmds[@]}"); reasons+=("oclab group"); }
user=$(id -un)
for group in dialout oclab; do
    if ! id -nG "$user" | tr ' ' '\n' | grep -qx "$group"; then
        root_cmds+=("usermod -aG $group $user"); reasons+=("$group membership")
    fi
done
if [ ${#root_cmds[@]} -gt 0 ]; then
    as_root "set up $(IFS=,; echo "${reasons[*]}" | sed 's/,/, /g')" "${root_cmds[@]}" || true
else
    note "udev rules and groups already set up"
fi
# New group membership only applies to new logins; sg applies it now.
pending=()
for group in dialout oclab; do
    if id -nG "$user" | tr ' ' '\n' | grep -qx "$group" && ! id -nG | tr ' ' '\n' | grep -qx "$group"; then
        pending+=("$group")
    fi
done
[ ${#pending[@]} -gt 0 ] && note "Group(s) ${pending[*]} not active in this login yet; using sg for this run (log out and in to make it permanent)."

with_groups() {  # with_groups cmd... : run with any pending groups applied
    local quoted
    quoted=$(printf '%q ' "$@")
    for group in "${pending[@]}"; do quoted="sg $group -c $(printf '%q' "$quoted")"; done
    eval "$quoted"
}

# 5. Weather station ------------------------------------------------------------
if [ "$demo" = 0 ]; then
    step "Weather station (Pico W)"
    if pgrep -x thonny >/dev/null 2>&1 || pgrep -f '[t]honny' >/dev/null 2>&1; then
        warn "Thonny is running and may hold the Pico's port. Close it (or disconnect it from the Pico)."
    fi
    probe_pico() { with_groups .venv/bin/python - <<'PY' || true
import sys, time
import serial
from serial.tools import list_ports
ports = sorted(p.device for p in list_ports.comports() if p.vid == 0x2E8A)
if not ports:
    print("absent -"); sys.exit()
try:
    s = serial.Serial(ports[0], 115200, timeout=0.3)
except (serial.SerialException, OSError) as exc:
    print("busy", ports[0], str(exc).replace("\n", " ")); sys.exit()
data, end = b"", time.monotonic() + 3
while time.monotonic() < end:
    data += s.read(4096)
s.close()
print("streaming" if b"\nW," in data else "silent", ports[0])
PY
    }
    probe=$(probe_pico)
    state=${probe%% *}; port=$(echo "$probe" | cut -d' ' -f2)
    case "$state" in
        streaming) note "Streaming on $port" ;;
        absent) warn "No Pico (USB VID 2e8a) found. Weather will be reported as missing until it is plugged in." ;;
        busy) warn "Cannot open the Pico port: ${probe#busy }. Is another program (Thonny, oclab) using it?" ;;
        silent) warn "The Pico on $port is connected but not sending data (firmware not running)." ;;
        *) warn "Could not check the Pico: $probe" ;;
    esac
    if { [ "$flash" = 1 ] || [ "$state" = silent ]; } && [ "$state" != absent ] && [ "$state" != busy ]; then
        if confirm "Install firmware/pico_weather/main.py on the Pico as main.py and restart it?"; then
            .venv/bin/python -c 'import mpremote' 2>/dev/null || .venv/bin/python -m pip install -q mpremote==1.29.0
            with_groups .venv/bin/python -m mpremote connect "$port" cp firmware/pico_weather/main.py :main.py + reset
            note "Firmware installed; checking the Pico restarts and streams..."
            sleep 3
            probe=$(probe_pico)
            if [ "${probe%% *}" = streaming ]; then
                note "Streaming again on ${probe#streaming }"
            else
                warn "The Pico is not streaming after the update ($probe). Check its output in Thonny."
            fi
        fi
    fi
fi

# 6. Start ---------------------------------------------------------------------------
if [ "$setup_only" = 1 ]; then
    step "Setup complete"; note "Start with ./run.sh"; exit 0
fi
args=(--config "$config")
[ "$demo" = 1 ] && args+=(--mode demo --data-dir ./data/demo)
step "Starting the monitor"
host=127.0.0.1
port_n=$(sed -n 's/^port[[:space:]]*=[[:space:]]*\([0-9]*\).*/\1/p' "$config" | head -1)
port_n=${port_n:-8000}
for ((i = 0; i < ${#passthrough[@]}; i++)); do
    case "${passthrough[i]}" in
        --host) host=${passthrough[i+1]:-$host} ;;
        --port) port_n=${passthrough[i+1]:-$port_n} ;;
    esac
done
note "Dashboard: http://$( [ "$host" = 0.0.0.0 ] && echo 127.0.0.1 || echo "$host" ):$port_n   (Ctrl+C to stop)"
with_groups .venv/bin/oclab "${args[@]}" "${passthrough[@]}"
