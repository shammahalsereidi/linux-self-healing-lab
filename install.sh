#!/usr/bin/env bash
# Install (or update) the Self-Healing Lab. All safety checks live in
# installer/lab_install.py. Usage: sudo ./install.sh [--dry-run]
set -euo pipefail
exec python3 "$(dirname "$(readlink -f "$0")")/installer/lab_install.py" install "$@"
