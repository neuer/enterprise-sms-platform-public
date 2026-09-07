"""无 lua 二进制时按同一 Redis 合同解释本单脚本。"""

from __future__ import annotations

from typing import Any


def eval_lua_subset(
    script: str,
    keys: list[str],
    args: list[str],
    *,
    now_sec: int,
    hashes: dict[str, dict[str, str]],
    kinds: dict[str, str],
) -> tuple[dict[str, dict[str, str]], dict[str, str], dict[str, int], Any]:
    store = _Store(hashes, kinds, now_sec)
    if "old_writers_fenced" in script and "rollback_finish" in script:
        result = _eval_cas(store, args)
    elif "window_total" in script and "now_sec - offset" in script:
        result = _eval_v1(store, keys, args)
    elif "marker_state ~= 'active_v2'" in script:
        result = _eval_weighted(store, keys, args, require_active=True)
    elif "if v1_rec > recipients then" in script:
        result = _eval_weighted(store, keys, args, require_active=False)
    else:
        raise RuntimeError("unsupported lua script for python port")
    return store.hashes, store.kinds, store.expires, result


class _Store:
    def __init__(
        self,
        hashes: dict[str, dict[str, str]],
        kinds: dict[str, str],
        now_sec: int,
    ) -> None:
        self.hashes = {key: dict(fields) for key, fields in hashes.items()}
        self.kinds = dict(kinds)
        self.expires: dict[str, int] = {}
        self.now_sec = now_sec

    def typ(self, key: str) -> str:
        return self.kinds.get(key, "none")

    def hget(self, key: str, field: str) -> str | None:
        if self.typ(key) == "none":
            return None
        if self.typ(key) != "hash":
            raise RuntimeError("WRONGTYPE")
        return self.hashes.get(key, {}).get(field)

    def hset(self, key: str, field: str, value: str) -> None:
        self.kinds[key] = "hash"
        self.hashes.setdefault(key, {})[field] = value

    def hincrby(self, key: str, field: str, amount: int) -> int:
        current = int(self.hget(key, field) or "0")
        next_value = current + amount
        self.hset(key, field, str(next_value))
        return next_value

    def expire(self, key: str, ttl: int) -> None:
        self.expires[key] = ttl


def _ring_total(store: _Store, key: str, now_sec: int) -> int:
    total = 0
    window_start = now_sec - 59
    for slot in range(60):
        epoch_raw = store.hget(key, f"e{slot}")
        epoch = int(epoch_raw) if epoch_raw is not None else None
        weight = int(store.hget(key, f"w{slot}") or "0")
        if epoch is not None and window_start <= epoch <= now_sec:
            total += weight
    return total


def _v1_active(store: _Store, key: str, now_sec: int) -> int | None:
    kind = store.typ(key)
    if kind == "none":
        return 0
    if kind != "hash":
        return None
    total = 0
    for epoch in range(now_sec - 59, now_sec + 1):
        raw = store.hget(key, str(epoch))
        if raw is None:
            continue
        try:
            weight = int(raw)
        except ValueError:
            return None
        if weight < 0:
            return None
        total += weight
    for epoch in range(now_sec + 1, now_sec + 60):
        if store.hget(key, str(epoch)) is not None:
            return None
    return total


def _eval_weighted(
    store: _Store,
    keys: list[str],
    args: list[str],
    *,
    require_active: bool,
) -> int:
    rec_key, seg_key, v1_rec_key, v1_seg_key, marker_key = keys
    rec_limit = int(args[0])
    rec_weight = int(args[1])
    seg_limit = int(args[2])
    seg_weight = int(args[3])
    ttl = int(args[4])
    now_sec = store.now_sec
    last_raw = store.hget(rec_key, "last_epoch")
    if last_raw is not None and now_sec < int(last_raw):
        now_sec = int(last_raw)
    if require_active:
        from app.services.app_ratelimit_cutover import CutoverError, parse_cutover_marker

        marker_typ = store.typ(marker_key)
        if marker_typ == "none":
            return -3
        if marker_typ != "hash":
            return -2
        try:
            marker = parse_cutover_marker(store.hashes[marker_key])
        except CutoverError:
            return -4
        if marker.requires_recovery or marker.target_writer_version != 2:
            return -4
        state = store.hget(marker_key, "state")
        schema = store.hget(marker_key, "schema_version")
        try:
            generation = int(store.hget(marker_key, "generation") or "")
        except ValueError:
            generation = 0
        try:
            fence = int(store.hget(marker_key, "fence_time") or "")
            after = int(store.hget(marker_key, "not_before") or "")
        except ValueError:
            return -4
        if (state != "active_v2" or schema != "1" or generation < 1
                or after < fence + 65 or store.now_sec < after):
            return -4
    try:
        v1_rec = _v1_active(store, v1_rec_key, now_sec)
        v1_seg = _v1_active(store, v1_seg_key, now_sec)
    except RuntimeError:
        return -2
    if v1_rec is None or v1_seg is None:
        return -2
    v2_rec = _ring_total(store, rec_key, now_sec)
    v2_seg = _ring_total(store, seg_key, now_sec)
    if max(v2_rec, v1_rec) + rec_weight > rec_limit:
        return 0
    if max(v2_seg, v1_seg) + seg_weight > seg_limit:
        return 0
    rec_add = rec_weight + max(0, v1_rec - v2_rec)
    seg_add = seg_weight + max(0, v1_seg - v2_seg)
    slot = now_sec % 60
    for key, weight in ((rec_key, rec_add), (seg_key, seg_add)):
        owned = store.hget(key, f"e{slot}")
        if owned != str(now_sec):
            store.hset(key, f"e{slot}", str(now_sec))
            store.hset(key, f"w{slot}", str(weight))
        else:
            store.hincrby(key, f"w{slot}", weight)
        store.hset(key, "last_epoch", str(now_sec))
        store.expire(key, ttl)
    if not require_active and store.hget(marker_key, "generation") is None:
        store.hset(marker_key, "schema_version", "2")
        store.hset(marker_key, "cutover_epoch", str(now_sec))
        store.hset(marker_key, "generation", "1")
        store.hset(marker_key, "state", "active")
        store.expire(marker_key, int(args[5]))
    return 1


def _eval_v1(store: _Store, keys: list[str], args: list[str]) -> int:
    rec_key, seg_key = keys
    now_sec = int(args[0])
    window = int(args[1])
    rec_limit = int(args[2])
    rec_weight = int(args[3])
    seg_limit = int(args[4])
    seg_weight = int(args[5])
    ttl = int(args[6])

    def window_total(key: str) -> int:
        total = 0
        for offset in range(window):
            raw = store.hget(key, str(now_sec - offset))
            total += int(raw or "0")
        expired = str(now_sec - window)
        if store.typ(key) == "hash":
            store.hashes.get(key, {}).pop(expired, None)
        return total

    if window_total(rec_key) + rec_weight > rec_limit:
        return 0
    if window_total(seg_key) + seg_weight > seg_limit:
        return 0
    store.hincrby(rec_key, str(now_sec), rec_weight)
    store.hincrby(seg_key, str(now_sec), seg_weight)
    store.expire(rec_key, ttl)
    store.expire(seg_key, ttl)
    return 1


def _eval_cas(store: _Store, args: list[str]) -> list[object]:
    from app.services.app_ratelimit_cutover import CutoverError, parse_cutover_marker

    action, expected_gen, expected_state, binding, target, minimum, reason, window, margin = args
    key = "ratelimit:cost:writer_cutover"
    now = store.now_sec
    kind = store.typ(key)
    if kind not in {"none", "hash"}:
        return [-2, "", "", str(now)]
    exists = kind == "hash"
    fields = store.hashes.get(key, {})
    generation, state = fields.get("generation", ""), fields.get("state", "")
    active = state in {"active_v1", "active_v2"}
    legacy = False

    def result(code: int) -> list[object]:
        return [code, state, generation, str(now)]

    if exists:
        try:
            marker = parse_cutover_marker(fields)
            legacy = marker.requires_recovery
        except CutoverError:
            return result(-2)
    try:
        valid = int(target) >= 1 and int(minimum) >= 1
    except ValueError:
        valid = False
    if not valid or not binding or reason != "writer_cutover" or window != "60" or margin != "5":
        return result(-2)
    if exists and (not expected_gen or not expected_state):
        return result(0)
    if expected_gen and expected_gen != generation or expected_state and expected_state != state:
        return result(0)
    begin = action in {"prepare", "rollback_prepare"}
    if begin and active and not legacy and fields["target_writer_version"] == target:
        return result(-5 if now < int(fields["not_before"]) else 2)
    new_operation = begin and (active or state == "aborted_closed")
    if (exists and not new_operation and action != "takeover_prepare"
            and fields["release_binding"] != binding):
        return result(-6)

    def write(gen: str, next_state: str, fence: str, after: str, min_version: str) -> list[object]:
        nonlocal generation, state, fields
        fields = {
            "schema_version": "1", "generation": gen, "state": next_state,
            "target_writer_version": target, "minimum_writer_version": min_version,
            "fence_time": fence, "not_before": after, "release_binding": binding,
            "admission_reason": reason, "window_seconds": window, "safety_margin_seconds": margin,
        }
        for name, value in fields.items():
            store.hset(key, name, value)
        generation, state = gen, next_state
        return result(1)

    if action == "takeover_prepare":
        if not exists or state != "preparing":
            return result(0)
        return write(str(int(generation)+1), "preparing", "", "", minimum)
    if begin:
        if not exists:
            if action == "rollback_prepare":
                return result(0)
            return write("1", "preparing", "", "", minimum)
        if new_operation:
            if int(minimum) < int(fields["minimum_writer_version"]):
                return result(-2)
            return write(str(int(generation)+1), "preparing", "", "", minimum)
        if fields["target_writer_version"] != target:
            return result(-6)
        return result(1 if state in {"preparing", "old_writers_fenced", "waiting_window"} else 0)
    if not exists or fields["target_writer_version"] != target:
        return result(0)
    if action == "invalidate_fence":
        if state not in {"old_writers_fenced", "waiting_window"}:
            return result(0)
        return write(generation, "preparing", "", "", minimum)
    if action in {"fence", "refence"}:
        if action == "fence" and state in {"old_writers_fenced", "waiting_window"}:
            return result(1)
        if state != "preparing" and not (action == "refence" and state in {
            "old_writers_fenced", "waiting_window",
        }):
            return result(0)
        return write(generation, "old_writers_fenced", str(now), str(now+65), minimum)
    if action == "wait":
        if state == "waiting_window":
            return result(1)
        if state != "old_writers_fenced":
            return result(0)
        if now < int(fields["fence_time"]):
            return result(-5)
        return write(
            generation, "waiting_window", fields["fence_time"], fields["not_before"], minimum,
        )
    if action in {"activate", "rollback_finish"}:
        if state != "waiting_window":
            return result(0)
        if now < int(fields["fence_time"]):
            return result(-5)
        if now < int(fields["not_before"]):
            return result(-3)
        return write(generation, "active_v1" if target == "1" else "active_v2",
                     fields["fence_time"], fields["not_before"], target)
    if action == "abort":
        return write(
            generation, "aborted_closed", fields["fence_time"], fields["not_before"], minimum,
        )
    return result(-4)
