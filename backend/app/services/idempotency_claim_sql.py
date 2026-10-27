"""幂等 Claim 的 SQL。从受理仓储拆出，避免单文件继续越过行数上限。"""

RESERVE_IDEMPOTENCY_CLAIM_SQL = """
INSERT INTO idempotency_claim (
  scope_kind, scope_id, biz_id, token, fingerprint,
  fingerprint_format_version, generation, expires_at, state
) VALUES (
  :scope_kind, :scope_id, :biz_id, :token, :fingerprint,
  2, 1, now() + make_interval(secs => :ttl_s), 'active'
)
ON CONFLICT (scope_kind, scope_id, biz_id)
DO UPDATE SET
  generation = idempotency_claim.generation + 1,
  token = EXCLUDED.token,
  fingerprint = EXCLUDED.fingerprint,
  fingerprint_format_version = EXCLUDED.fingerprint_format_version,
  expires_at = EXCLUDED.expires_at,
  state = 'active',
  batch_id = NULL,
  completed_at = NULL,
  result_expires_at = NULL,
  released_at = NULL,
  release_reason = NULL,
  updated_at = now()
WHERE idempotency_claim.state IN ('expired','released')
   OR (
     idempotency_claim.state='active'
     AND idempotency_claim.expires_at < now()
   )
RETURNING generation
"""

RENEW_IDEMPOTENCY_CLAIM_SQL = """
UPDATE idempotency_claim SET
  expires_at=now()+make_interval(secs=>:ttl_s),
  updated_at=now()
WHERE scope_kind=:scope_kind AND scope_id=:scope_id
  AND biz_id=:biz_id AND token=:token
  AND fingerprint=:fingerprint
  AND generation=:generation
  AND state='active'
  AND expires_at>now()-make_interval(secs=>:margin_s)
RETURNING generation
"""

RELEASE_IDEMPOTENCY_CLAIM_SQL = """
UPDATE idempotency_claim SET
  state='released',
  released_at=now(),
  release_reason=:reason,
  updated_at=now()
WHERE scope_kind=:scope_kind AND scope_id=:scope_id
  AND biz_id=:biz_id AND token=:token
  AND generation=:generation
  AND state='active'
  AND batch_id IS NULL
  AND NOT EXISTS (
    SELECT 1 FROM idempotency_record r
    WHERE r.scope_kind=idempotency_claim.scope_kind
      AND r.scope_id=idempotency_claim.scope_id
      AND r.biz_id=idempotency_claim.biz_id
      AND r.expires_at>now()
  )
RETURNING id
"""
