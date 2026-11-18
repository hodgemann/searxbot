#!/usr/bin/env bash
# Launch SearxBot using the already-running SearXNG service.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
pybot_checkout="${PYBOT_PATH:-${script_dir}/../pybot}"

exec python3 -B "${script_dir}/pybot_search.py" --pybot-path "${pybot_checkout}" "$@"
