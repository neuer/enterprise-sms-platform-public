#!/bin/sh
# 在迁移前为七个运行身份设置独立 LOGIN 密码；密码只经 secret 文件进入 psql 内存。
set -eu

owner_secret="/run/secrets/db_owner_password"
pgpass_file="/tmp/db-role-provision.pgpass"
umask 077
owner_password="$(tr -d '\r\n' < "$owner_secret")"
printf '%s:%s:%s:%s:%s\n' \
  "${DB_HOST:-postgres}" \
  "${DB_PORT:-5432}" \
  "${DB_NAME:-sms}" \
  sms_owner \
  "$owner_password" >"$pgpass_file"
unset owner_password
export PGPASSFILE="$pgpass_file"

psql -X -v ON_ERROR_STOP=1 \
  --host "${DB_HOST:-postgres}" \
  --port "${DB_PORT:-5432}" \
  --username sms_owner \
  --dbname "${DB_NAME:-sms}" <<'SQL'
\set QUIET on
\set auth_password `tr -d '\r\n' < /run/secrets/db_auth_password`
\set accept_password `tr -d '\r\n' < /run/secrets/db_accept_password`
\set send_password `tr -d '\r\n' < /run/secrets/db_send_password`
\set callback_password `tr -d '\r\n' < /run/secrets/db_callback_password`
\set export_password `tr -d '\r\n' < /run/secrets/db_export_password`
\set scheduler_password `tr -d '\r\n' < /run/secrets/db_scheduler_password`
\set metrics_password `tr -d '\r\n' < /run/secrets/db_metrics_password`

DO $roles$
DECLARE
  role_name text;
BEGIN
  FOREACH role_name IN ARRAY ARRAY[
    'sms_auth','sms_accept','sms_send','sms_callback',
    'sms_export','sms_scheduler','sms_metrics'
  ]
  LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=role_name) THEN
      EXECUTE format(
        'CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT',
        role_name
      );
    END IF;
  END LOOP;
END
$roles$;

SELECT format(
  'ALTER ROLE sms_auth LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT PASSWORD %L',
  :'auth_password'
) \gexec
SELECT format(
  'ALTER ROLE sms_accept LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT PASSWORD %L',
  :'accept_password'
) \gexec
SELECT format(
  'ALTER ROLE sms_send LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT PASSWORD %L',
  :'send_password'
) \gexec
SELECT format(
  'ALTER ROLE sms_callback LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT PASSWORD %L',
  :'callback_password'
) \gexec
SELECT format(
  'ALTER ROLE sms_export LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT PASSWORD %L',
  :'export_password'
) \gexec
SELECT format(
  'ALTER ROLE sms_scheduler LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT PASSWORD %L',
  :'scheduler_password'
) \gexec
SELECT format(
  'ALTER ROLE sms_metrics LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT PASSWORD %L',
  :'metrics_password'
) \gexec
SELECT format(
  'REVOKE CONNECT, TEMPORARY ON DATABASE %I FROM PUBLIC',
  current_database()
) \gexec
SELECT format(
  'GRANT CONNECT ON DATABASE %I TO sms_owner,sms_auth,sms_accept,sms_send,sms_callback,sms_export,sms_scheduler,sms_metrics',
  current_database()
) \gexec
SQL

rm -f "$pgpass_file"
