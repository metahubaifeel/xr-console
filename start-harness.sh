#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m harness --mode live --port "${HARNESS_PORT:-8910}"
