#!/bin/sh
# 仅安装当前架构的已签名、已固定哈希的软件包；任何缺失或异常均失败关闭。
set -eu
arch="$(apk --print-arch)"
case "$arch" in
  x86_64|aarch64) ;;
  *) echo 'unsupported retained APK architecture' >&2; exit 1 ;;
esac
cd "/apks/$arch"
test -s SHA256SUMS
sha256sum -c SHA256SUMS
# 清单条目数量必须等于安装集合，避免未列入清单的包被通配符选中。
set -- ./*.apk
test -f "$1"
test "$(wc -l < SHA256SUMS | tr -d ' ')" -eq "$#"
test "$(sha256sum "$@" | sed 's@  ./@  @' | sort)" = "$(sort SHA256SUMS)"
apk verify "$@"
apk --repositories-file /dev/null add --no-cache --no-network "$@"
