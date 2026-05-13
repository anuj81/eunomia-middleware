#!/usr/bin/env bash
# Launch the Eunomia middleware.
#
# Secrets are loaded from .env (gitignored) — copy .env.example → .env and fill
# in real values. CLI flags override .env / YAML / defaults:
#
#   ./start-fastapi.sh --verbose DEBUG --openmetadata-mock
#   ./start-fastapi.sh --config config/eunomia.local.yaml --no-openmetadata-mock
#
set -euo pipefail
cd "$(dirname "$0")"
source venv/bin/activate
exec python -m src.main "$@"
