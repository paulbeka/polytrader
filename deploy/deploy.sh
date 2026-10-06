#!/bin/sh
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export PYTHONPATH="$script_dir/../src${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m polytrader.ops.deployment "$@"
