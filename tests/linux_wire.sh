#!/bin/sh
# Disposable Linux x64 validation. The source mount is read-only in both phases.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
volume="deepseek-bridge-wire-$$"
image="python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0"
docker volume create "$volume" >/dev/null
trap 'docker volume rm "$volume" >/dev/null' EXIT HUP INT TERM
docker run --rm --platform linux/amd64 \
  --mount "type=bind,source=$root,target=/source,readonly" \
  --mount "type=volume,source=$volume,target=/env" -w /source "$image" \
  sh -c 'python -m pip install --disable-pip-version-check uv==0.5.9 && UV_PROJECT_ENVIRONMENT=/env uv sync --frozen'
docker run --rm --platform linux/amd64 --network none --cap-drop ALL \
  --security-opt no-new-privileges \
  --mount "type=bind,source=$root,target=/source,readonly" \
  --mount "type=volume,source=$volume,target=/env" \
  -e BRIDGE_WIRE_SANDBOX=1 -e PYTHONDONTWRITEBYTECODE=1 -w /source "$image" \
  /env/bin/python -m pytest -m wire -p no:cacheprovider -q -s --tb=short --basetemp /tmp/wire
