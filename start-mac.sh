#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
robot_ip="${ROBOT_IP:-10.230.20.13}"
robot_user="${ROBOT_SSH_USER:-xr}"
control_path="${TMPDIR:-/tmp}/xr-console-${robot_user}-${robot_ip}.sock"
if ! ssh -S "$control_path" -O check "$robot_user@$robot_ip" 2>/dev/null; then
  ssh -M -S "$control_path" -o ControlPersist=8h -o ConnectTimeout=10 -fN "$robot_user@$robot_ip"
fi
export ROBOT_SSH_USER="$robot_user"
export ROBOT_SSH_CONTROL_PATH="$control_path"
exec python3 -u 控制台.py --ip "$robot_ip" --host 127.0.0.1 --port "${PORT:-8900}"
