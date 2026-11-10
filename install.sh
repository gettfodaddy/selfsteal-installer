#!/usr/bin/env bash
set -Eeuo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if ! command -v python3 >/dev/null 2>&1; then
  printf '%s\n' 'Нужен Python 3: apt update && apt install -y python3' >&2
  exit 1
fi
exec python3 ./selfsteal.py "$@"
