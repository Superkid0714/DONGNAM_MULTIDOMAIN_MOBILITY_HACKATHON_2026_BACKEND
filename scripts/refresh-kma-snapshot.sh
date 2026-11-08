#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run python -m freshswitch_backend.integrations.kma_forecast "$@"
