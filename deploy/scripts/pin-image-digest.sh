#!/usr/bin/env bash
# =============================================================================
# Resolve the digest-pinned reference for an image already present on the host.
#
#   deploy/scripts/pin-image-digest.sh ghcr.io/tencentcloud/octop:1.2.3
#
# Prints the single `repo@sha256:<digest>` reference to put in OCTOP_IMAGE, or
# fails closed when the image is absent locally or has no repository digest
# (a locally built image that was never pushed has none — tag it into a registry
# you control, then re-run).
# =============================================================================
set -euo pipefail

SELF_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=./lib.sh
. "${SELF_DIR}/lib.sh"

[ $# -eq 1 ] || fail DEPLOYMENT_POLICY "usage: pin-image-digest.sh <image-tag>"
IMAGE="$1"

case "$IMAGE" in
    *@sha256:*) fail DEPLOYMENT_POLICY "reference is already digest-pinned" "image=$IMAGE" ;;
esac

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    fail DEPENDENCY_UNAVAILABLE "image is not present on this host" "image=$IMAGE"
fi

digest=$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$IMAGE" \
    | grep '@sha256:' | head -n 1 | tr -d '\r' || true)
if [ -z "$digest" ]; then
    fail DEPENDENCY_UNAVAILABLE \
        "image has no repository digest (push it to a registry first)" "image=$IMAGE"
fi

printf '{"status":"passed","service":"pin-image-digest","tag":"%s","pinned":"%s"}\n' \
    "$(json_escape "$IMAGE")" "$(json_escape "$digest")"
