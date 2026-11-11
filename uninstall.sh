#!/usr/bin/env bash
# Remove only what the installer created (per its manifest).
# Usage: sudo ./uninstall.sh [--dry-run]
set -euo pipefail
exec python3 "$(dirname "$(readlink -f "$0")")/installer/lab_install.py" uninstall "$@"
