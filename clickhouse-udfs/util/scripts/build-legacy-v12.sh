#!/usr/bin/env bash
# Build only the historical v12 JSONDropKeys implementation, never util/cmd's replacement.
# Usage: bash build-legacy-v12.sh SOURCE_ARCHIVE OUTPUT_DIRECTORY
# Download SOURCE_ARCHIVE from the immutable URL in legacy-v12-provenance.json.
# Requires Go 1.27.3, sha256sum, tar, and a writable external output directory.
set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo "usage: $0 SOURCE_ARCHIVE OUTPUT_DIRECTORY" >&2
    exit 2
fi

SOURCE_ARCHIVE=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
mkdir -p "$2"
OUT_DIR=$(cd "$2" && pwd)
REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
case "$OUT_DIR/" in
    "$REPO_DIR/"*) echo "output must be outside the source checkout" >&2; exit 2 ;;
esac

export GOTOOLCHAIN=local
export GOFLAGS='' GOEXPERIMENT='' GOWORK=off
export GOPROXY=https://proxy.golang.org GOSUMDB=sum.golang.org
if [[ $(go env GOVERSION) != go1.27.3 ]]; then
    echo "Go 1.27.3 is required; install the checksum-pinned official toolchain" >&2
    exit 1
fi
printf '%s  %s\n' \
    f2520b38913c967042771fe5f3ee70810ec0345f16717e5da2cca0ac40da00d1 \
    "$SOURCE_ARCHIVE" | sha256sum --check --status

WORK_DIR=$(mktemp -d)
trap 'rm -rf "$WORK_DIR"' EXIT
tar -xzf "$SOURCE_ARCHIVE" --strip-components=1 -C "$WORK_DIR"
cd "$WORK_DIR"
# The archived module and dependency checksums remain unmodified.
go mod download
for goarch in amd64 arm64; do
    case "$goarch" in
        amd64) suffix=x86_64 ;;
        arm64) suffix=aarch64 ;;
    esac
    CGO_ENABLED=0 GOOS=linux GOARCH="$goarch" GOAMD64=v1 GOARM64=v8.0 \
        go build -mod=readonly -buildvcs=false -trimpath -ldflags '-s -w -buildid=' \
        -o "$OUT_DIR/json_drop_keys_udf_${suffix}" ./cmd/json_drop_keys_udf
done
# Keep provenance alongside external build outputs, not inside the checkout.
cp "$REPO_DIR/clickhouse-udfs/util/scripts/legacy-v12-provenance.json" "$OUT_DIR/"
