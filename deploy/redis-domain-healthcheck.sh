#!/bin/sh
set -eu

domain="${1:-}"
case "$domain" in
  broker | auth | control) ;;
  *) exit 2 ;;
esac

secret_file="/run/secrets/redis_${domain}_password"
username="sms_${domain}"
{
  cat "$secret_file"
  printf '\n'
} | redis-cli --user "$username" --askpass --raw ping 2>/dev/null | grep -qx PONG
