#!/usr/bin/env bash
# potluck's own gate: the tool's test suites must pass. This is what makes
# potluck able to build potluck -- `potluck fix` drives changes to this repo
# and this gate settles correctness.
set -e
cd "$(dirname "$0")/.."
./test
