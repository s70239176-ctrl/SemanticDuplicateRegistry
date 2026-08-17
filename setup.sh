#!/usr/bin/env bash
#
# setup.sh — bootstrap for SemanticDuplicateRegistry
#
# Installs the GenLayer CLI (requires Node.js 18+) and opens GenLayer
# Studio in the browser so you can deploy semantic_duplicate_registry.py
# without any manual setup steps beyond this script.
#
# Usage:
#   chmod +x setup.sh
#   ./setup.sh

set -euo pipefail

echo "== SemanticDuplicateRegistry setup =="

if ! command -v node >/dev/null 2>&1; then
  echo "Node.js is required (v18+) but was not found on PATH."
  echo "Install it from https://nodejs.org and re-run this script."
  exit 1
fi

echo "-- Installing GenLayer CLI globally via npm --"
npm install -g genlayer

echo "-- Launching GenLayer Studio --"
echo "This opens a new browser tab with the Studio sandbox."
genlayer init studio || genlayer up

cat <<'EOF'

Next steps:
  1. In Studio, open "Run and Deploy" and load semantic_duplicate_registry.py.
  2. Paste a funded account address into the constructor's "admin" field
     (a default is pre-filled in the source, but override it with your
     own address for a real test run).
  3. Click Deploy.
  4. Follow the manual test plan in README.md.

Deployment metadata (network, constructor args, recorded tx hashes) is
in genlayer.config.json.
EOF
