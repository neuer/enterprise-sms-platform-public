#!/bin/sh
set -eu

domain="${1:-}"
case "$domain" in
  broker | auth | control) ;;
  redis-server)
    exec docker-entrypoint.sh "$@"
    ;;
  *)
    echo "redis domain must be broker, auth or control" >&2
    exit 2
    ;;
esac

secret_file="/run/secrets/redis_${domain}_password"
username="sms_${domain}"
if [ ! -f "$secret_file" ] || [ -L "$secret_file" ]; then
  echo "redis ACL secret is unavailable" >&2
  exit 1
fi
password="$(cat "$secret_file")"
if [ "${#password}" -lt 32 ] || [ "${#password}" -gt 128 ]; then
  echo "redis ACL secret has invalid format" >&2
  exit 1
fi
case "$password" in
  *[!A-Za-z0-9_+/=-]*)
    echo "redis ACL secret has invalid format" >&2
    exit 1
    ;;
esac

case "$domain" in
  broker)
    maxmemory='384mb'
    key_rules='~realtime ~realtime* ~bulk ~bulk* ~callback ~callback* ~celery* ~_kombu* ~unacked ~unacked_index ~unacked_mutex ~*.reply.celery.pidbox*'
    command_rules='+ping +client|id +client|getname +client|setname +client|setinfo +client|getredir +get +set +setex +psetex +del +exists +expire +pexpire +ttl +pttl +incr +decr +mget +mset +lpush +rpush +lpop +rpop +llen +brpop +rpoplpush +brpoplpush +zadd +zrem +zrange +zrevrange +zrangebyscore +zrevrangebyscore +zremrangebyscore +zscore +hget +hset +hdel +hgetall +hkeys +sadd +srem +smembers +publish +subscribe +psubscribe +unsubscribe +punsubscribe +eval +evalsha +script|load +multi +exec +discard +watch +unwatch'
    ;;
  auth)
    maxmemory='192mb'
    key_rules='~auth:* ~export:step-up:* ~vendor-test:step-up:*'
    command_rules='+ping +get +set +del +exists +incr +expire +pexpire +ttl +pttl +hget +hset +hsetnx +hdel +hmget +hgetall +type +persist +zadd +zrem +zrange +zrangebyscore +zcard +time +eval +evalsha'
    ;;
  control)
    maxmemory='192mb'
    key_rules='~acceptance:* ~admission:* ~alert:* ~anomaly:* ~app:* ~approval:* ~batch:* ~blacklist:* ~callback:* ~dept:* ~freq:* ~idem:* ~lock:* ~queue:paused:* ~quota:* ~ratelimit:* ~scheduled:* ~sms-platform:* ~uat:* ~usage:* ~vuat:*'
    command_rules='+ping +get +set +del +mget +exists +incr +incrby +expire +pexpire +pexpireat +ttl +pttl +hget +hset +hincrby +hdel +hgetall +hmget +type +sadd +srem +smembers +smismember +zadd +zcard +zrange +zrem +zremrangebyscore +time +eval +evalsha +script|load +multi +exec +discard +watch +unwatch +scan'
    ;;
esac

acl_file="/run/redis/users.acl"
umask 077
mkdir -p /run/redis
{
  printf 'user default off\n'
  printf 'user %s on >%s %s &* %s\n' \
    "$username" "$password" "$key_rules" "$command_rules"
} >"$acl_file"
unset password key_rules command_rules

set -- \
  --aclfile "$acl_file" \
  --appendonly yes \
  --appendfsync everysec \
  --maxmemory "$maxmemory" \
  --maxmemory-policy noeviction \
  --protected-mode yes

tls_required="${REDIS_TLS_REQUIRED:-0}"
case "$tls_required" in
  0) ;;
  1)
    tls_key='/run/secrets/redis_tls_server_key'
    tls_cert='/run/redis-tls/server.pem'
    tls_ca='/run/redis-tls/ca.pem'
    for tls_file in "$tls_key" "$tls_cert" "$tls_ca"; do
      if [ ! -f "$tls_file" ] || [ -L "$tls_file" ]; then
        echo "redis TLS material is unavailable" >&2
        exit 1
      fi
    done
    set -- "$@" \
      --port 0 \
      --tls-port 6379 \
      --tls-cert-file "$tls_cert" \
      --tls-key-file "$tls_key" \
      --tls-ca-cert-file "$tls_ca" \
      --tls-auth-clients no
    ;;
  *)
    echo "REDIS_TLS_REQUIRED must be 0 or 1" >&2
    exit 2
    ;;
esac

unset maxmemory tls_required
exec docker-entrypoint.sh redis-server "$@"
