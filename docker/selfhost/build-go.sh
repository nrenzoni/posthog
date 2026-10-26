#!/usr/bin/env bash
set -euo pipefail

# This image's pinned Go stage must agree with the legacy build's toolchain check.
export GOTOOLCHAIN=local GOWORK=off
export GOPROXY=https://proxy.golang.org GOSUMDB=sum.golang.org
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

curl --fail --location --proto '=https' --proto-redir '=https' --tlsv1.2 \
    --retry 3 --connect-timeout 20 --max-time 300 \
    --output "$work/legacy.tar.gz" \
    https://codeload.github.com/PostHog/clickhouse-json-drop-keys-udf/tar.gz/2d235efbaa824368580749edc00064d14c60195a
bash /code/clickhouse-udfs/util/scripts/build-legacy-v12.sh "$work/legacy.tar.gz" /artifacts/v12

curl --fail --location --proto '=https' --proto-redir '=https' --tlsv1.2 \
    --retry 3 --connect-timeout 20 --max-time 300 \
    --output "$work/esbuild.tar.gz" \
    https://codeload.github.com/evanw/esbuild/tar.gz/609683d892977362a0f99026cb74b96263d728a9
printf '%s  %s\n' \
    04cb2d38fb6af093577aa8bf174add7286099b65ef7854480cbbb8ff061cef1f \
    "$work/esbuild.tar.gz" | sha256sum --check --strict -
mkdir "$work/esbuild" /artifacts/esbuild
tar --extract --gzip --file "$work/esbuild.tar.gz" --strip-components=1 --directory "$work/esbuild"
cd "$work/esbuild"
for arch in amd64 arm64; do
    CGO_ENABLED=0 GOOS=linux GOARCH="$arch" GOAMD64=v1 GOARM64=v8.0 \
        go build -mod=readonly -buildvcs=false -trimpath -ldflags '-s -w -buildid=' \
        -o "/artifacts/esbuild/$arch" ./cmd/esbuild
done
