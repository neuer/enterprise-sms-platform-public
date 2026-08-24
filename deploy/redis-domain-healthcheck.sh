#!/bin/sh
set -eu

domain="${1:-}"
case "$domain" in
  broker | auth | control) ;;
  *) exit 2 ;;
esac

secret_file="/run/secrets/redis_${domain}_password"
username="sms_${domain}"
set -- --user "$username" --askpass --raw
case "${REDIS_TLS_REQUIRED:-0}" in
  0) ;;
  1)
    case "$domain" in
      broker) server_name='redis' ;;
      auth) server_name='redis-auth' ;;
      control) server_name='redis-control' ;;
    esac
    set -- \
      "$@" \
      --tls \
      --cacert /run/redis-tls/ca.pem \
      --sni "$server_name" \
      -h "$server_name"
    ;;
  *) exit 2 ;;
esac
{
  cat "$secret_file"
  printf '\n'
} | redis-cli "$@" ping 2>/dev/null | grep -qx PONG
