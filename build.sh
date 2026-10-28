#!/bin/bash
# Builds the image and pushes it to the registry on the homeserver.
#
# Usage: ./build.sh
# Override the target with IMAGE=... and/or TAG=... if needed.
set -euo pipefail

IMAGE="${IMAGE:-homeserver.lan:5000/networth}"
TAG="${TAG:-latest}"

# --pull always fetches the newest base image (security updates)
docker build --pull -t "${IMAGE}:${TAG}" .
docker login "${IMAGE%%/*}"
docker push "${IMAGE}:${TAG}"
