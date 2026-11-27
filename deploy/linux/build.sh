#!/usr/bin/env bash
# 构建 Linux amd64 原生包：需要 Docker，输出 dist/ptbrush-linux-amd64.tar.gz
set -euo pipefail
cd "$(dirname "$0")/../.."

VERSION="$(cat VERSION)"
echo "版本：$VERSION"

docker build -f deploy/linux/Dockerfile.build -t ptbrush-linux-build .

CID="$(docker create ptbrush-linux-build)"
trap 'docker rm -f "$CID" >/dev/null 2>&1 || true' EXIT
rm -rf dist
mkdir -p dist
docker cp "$CID:/build/dist/ptbrush" dist/ptbrush
docker rm "$CID" >/dev/null
trap - EXIT

install -m 755 deploy/linux/install.sh dist/ptbrush/install.sh
install -m 755 deploy/linux/menu.sh dist/ptbrush/menu.sh

tar -czf dist/ptbrush-linux-amd64.tar.gz -C dist ptbrush
( cd dist && sha256sum ptbrush-linux-amd64.tar.gz > ptbrush-linux-amd64.tar.gz.sha256 )
ls -lh dist/ptbrush-linux-amd64.tar.gz
cat dist/ptbrush-linux-amd64.tar.gz.sha256