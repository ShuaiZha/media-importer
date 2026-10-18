#!/bin/sh
# Bootstrap only: downloads a native stable bundle, no Python/Docker dependency.
set -eu
base='https://github.com/ShuaiZha/media-importer/releases'
case "$(uname -s)" in Darwin) system=macos ;; Linux) system=linux ;; *) echo '请使用 Windows 安装脚本。' >&2; exit 1 ;; esac
case "$(uname -m)" in arm64|aarch64) arch=arm64 ;; x86_64) arch=amd64 ;; *) echo '暂不支持此处理器。' >&2; exit 1 ;; esac
temp=$(mktemp -d)
trap 'rm -rf "$temp"' EXIT HUP INT TERM
curl --proto '=https' --tlsv1.2 -fsSL "$base/latest/download/native-$system-$arch.json" -o "$temp/release.json"
version=$(sed -n 's/.*"version": "\([0-9][0-9.]*\)".*/\1/p' "$temp/release.json")
digest=$(sed -n 's/.*"sha256": "\([0-9a-f]*\)".*/\1/p' "$temp/release.json")
case "$version" in ''|*[!0-9.]*) echo '发行清单无效。' >&2; exit 1 ;; esac
test "${#digest}" = 64 || { echo '缺少安装包校验值。' >&2; exit 1; }
name="media-importer-$version-$system-$arch.tar.gz"
curl --proto '=https' --tlsv1.2 -fsSL "$base/download/v$version/$name" -o "$temp/package.tar.gz"
if command -v sha256sum >/dev/null 2>&1; then
  actual=$(sha256sum "$temp/package.tar.gz" | cut -d ' ' -f 1)
else
  actual=$(shasum -a 256 "$temp/package.tar.gz" | cut -d ' ' -f 1)
fi
test "$actual" = "$digest" || { echo '安装包校验失败，未安装。' >&2; exit 1; }
tar -xzf "$temp/package.tar.gz" -C "$temp"
existing_project=false
for argument in "$@"; do
  case "$argument" in --project|--project=*) existing_project=true ;; esac
done
if [ -t 0 ] || [ "$existing_project" = true ]; then
  "$temp/media-importer/media-importer" install "$@"
else
  "$temp/media-importer/media-importer" install "$@" </dev/tty
fi
