#!/usr/bin/env bash
cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1
set -eu

# No owner, registry credential, or private dependency is baked into this repo.
: "${RUNNER_IMAGE_REPOSITORY:?Export the target image repository (without a tag)}"
NO_CACHE=""
for arg in "$@"; do
    if [ "$arg" = "--no-cache" ]; then
        NO_CACHE="--no-cache"
    else
        echo "Unknown argument: $arg" >&2
        exit 1
    fi
done
VERSION_FILE=VERSION
if [ ! -f "$VERSION_FILE" ]; then
    echo 0 > "$VERSION_FILE"
fi
CURRENT_VERSION=$(cat "$VERSION_FILE")
if ! echo "$CURRENT_VERSION" | grep -qE '^[0-9]+$'; then
    echo "VERSION must contain a nonnegative integer" >&2
    exit 1
fi
VERSION=$((CURRENT_VERSION + 1))
docker build $NO_CACHE --platform linux/amd64 -t "$RUNNER_IMAGE_REPOSITORY:$VERSION" . || exit 1
docker tag "$RUNNER_IMAGE_REPOSITORY:$VERSION" "$RUNNER_IMAGE_REPOSITORY:latest" || exit 1
docker push "$RUNNER_IMAGE_REPOSITORY:$VERSION" || exit 1
docker push "$RUNNER_IMAGE_REPOSITORY:latest" || exit 1
echo "$VERSION" > "$VERSION_FILE"
docker inspect --format '{{join .RepoDigests "\n"}}' "$RUNNER_IMAGE_REPOSITORY:$VERSION"
