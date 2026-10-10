#!/usr/bin/env bash
# Provide localhost/reschema-toolchain:1. Pull the build CI published to GHCR
# for this exact Containerfile (tag = its sha256 prefix); build locally only
# when no published image exists. A pulled image keeps CI's image ID, so the
# toolchain stamped in ledger audits (#146) matches across machines; every
# local build gets a fresh ID.
#
#   tools/toolchain_image.sh          pull, else build
#   tools/toolchain_image.sh --tag    print the tag only (CI uses it)
set -euo pipefail
cd "$(dirname "$0")/.."
TAG=$(sha256sum Containerfile | cut -c1-16)
[[ "${1:-}" == --tag ]] && { echo "$TAG"; exit 0; }
REMOTE=ghcr.io/lewdwig-v/reschema-toolchain:$TAG
LOCAL=localhost/reschema-toolchain:1
if podman pull "$REMOTE"; then
    podman tag "$REMOTE" "$LOCAL"
else
    echo "no published image for $TAG; building locally" >&2
    podman build -t "$LOCAL" -f Containerfile .
fi
