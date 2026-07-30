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
if ! grep -Eq '^[A-Za-z0-9_+/=-]{32,172}$' "$secret_file"; then
  echo "redis ACL secret has invalid format" >&2
  exit 1
fi

case "$domain" in
  broker)
    key_rules='~realtime ~realtime* ~bulk ~bulk* ~callback ~callback* ~celery* ~_kombu* ~unacked ~unacked_index ~unacked_mutex ~*.reply.celery.pidbox*'
    command_rules='+ping +client +get +set +setex +psetex +del +exists +expire +pexpire +ttl +pttl +incr +decr +mget +mset +lpush +rpush +lpop +rpop +llen +brpop +rpoplpush +brpoplpush +zadd +zrem +zrange +zrevrange +zrangebyscore +zrevrangebyscore +zremrangebyscore +zscore +hget +hset +hdel +hgetall +hkeys +sadd +srem +smembers +publish +subscribe +psubscribe +unsubscribe +punsubscribe +eval +evalsha +script|load +multi +exec +discard +watch +unwatch'
    ;;
  auth)
    key_rules='~auth:* ~export:step-up:* ~vendor-test:step-up:*'
    command_rules='+ping +get +set +del +incr +expire +eval +evalsha'
    ;;
  control)
    key_rules='~acceptance:* ~alert:* ~anomaly:* ~app:* ~approval:* ~batch:* ~blacklist:* ~callback:* ~dept:* ~freq:* ~idem:* ~lock:* ~queue:paused:* ~quota:* ~ratelimit:* ~scheduled:* ~sms-platform:* ~uat:* ~usage:* ~vuat:*'
    command_rules='+ping +get +set +del +mget +exists +incr +incrby +expire +pexpire +pexpireat +ttl +pttl +hget +hset +hdel +hgetall +hmget +sadd +srem +smembers +smismember +zadd +zcard +zrem +zremrangebyscore +eval +evalsha +script|load +multi +exec +discard +watch +unwatch +scan'
    ;;
esac

password="$(cat "$secret_file")"
acl_file="/run/redis/users.acl"
umask 077
mkdir -p /run/redis
{
  printf 'user default off\n'
  printf 'user %s on >%s %s &* %s\n' \
    "$username" "$password" "$key_rules" "$command_rules"
} >"$acl_file"
unset password key_rules command_rules

exec docker-entrypoint.sh redis-server \
  --aclfile "$acl_file" \
  --appendonly yes \
  --appendfsync everysec \
  --maxmemory-policy noeviction \
  --protected-mode yes
