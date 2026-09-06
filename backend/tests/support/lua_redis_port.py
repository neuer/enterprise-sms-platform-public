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
        marker_typ = store.typ(marker_key)
        if marker_typ == "none":
            return -3
        if marker_typ != "hash":
            return -2
        state = store.hget(marker_key, "state")
        schema = store.hget(marker_key, "schema_version")
        try:
            generation = int(store.hget(marker_key, "generation") or "")
        except ValueError:
            generation = 0
        if state != "active_v2" or schema != "1" or generation < 1:
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
    action = args[0]
    expect_generation = args[1]
    expect_state = args[2]
    release_binding = args[3]
    target_writer = args[4]
    min_writer = args[5]
    admission_reason = args[6]
    window_seconds = args[7]
    safety_margin = args[8]
    marker = "ratelimit:cost:writer_cutover"
    now_sec = store.now_sec

    def field(name: str) -> str:
        raw = store.hget(marker, name)
        return "" if raw is None else str(raw)

    typ = store.typ(marker)
    if typ not in {"none", "hash"}:
        return [-2, "", "", str(now_sec)]
    exists = typ == "hash"
    current_generation = field("generation")
    current_state = field("state")
    if exists:
        if field("schema_version") != "1":
            return [-2, current_state, current_generation, str(now_sec)]
        if current_generation == "" or current_state == "":
            return [-2, current_state, current_generation, str(now_sec)]
        try:
            if int(current_generation) < 1:
                return [-2, current_state, current_generation, str(now_sec)]
        except ValueError:
            return [-2, current_state, current_generation, str(now_sec)]
        if current_state not in {
            "preparing",
            "old_writers_fenced",
            "waiting_window",
            "active_v2",
            "aborted_closed",
        }:
            return [-2, current_state, current_generation, str(now_sec)]
        bound = field("release_binding")
        if bound not in {"", release_binding}:
            return [-6, current_state, current_generation, str(now_sec)]
    if expect_generation and (not exists or current_generation != expect_generation):
        return [0, current_state, current_generation, str(now_sec)]
    if expect_state and (not exists or current_state != expect_state):
        return [0, current_state, current_generation, str(now_sec)]

    def write_fields(
        generation: str,
        state: str,
        fence_time: str,
        not_before: str,
        minimum: str,
    ) -> None:
        store.hset(marker, "schema_version", "1")
        store.hset(marker, "generation", generation)
        store.hset(marker, "target_writer_version", target_writer)
        store.hset(marker, "minimum_writer_version", minimum)
        store.hset(marker, "fence_time", fence_time)
        store.hset(marker, "not_before", not_before)
        store.hset(marker, "state", state)
        store.hset(marker, "release_binding", release_binding)
        store.hset(marker, "admission_reason", admission_reason)
        store.hset(marker, "window_seconds", window_seconds)
        store.hset(marker, "safety_margin_seconds", safety_margin)

    if action == "prepare":
        if (
            exists
            and current_state == "active_v2"
            and field("target_writer_version") == target_writer
        ):
            return [1, current_state, current_generation, str(now_sec)]
        if exists and current_state in {
            "preparing",
            "old_writers_fenced",
            "waiting_window",
        }:
            return [1, current_state, current_generation, str(now_sec)]
        generation = "1"
        if exists:
            generation = str(int(current_generation))
            if current_state == "aborted_closed":
                generation = str(int(current_generation) + 1)
            else:
                return [0, current_state, current_generation, str(now_sec)]
        write_fields(generation, "preparing", "", "", min_writer)
        return [1, "preparing", generation, str(now_sec)]
    if action == "fence":
        if exists and current_state in {
            "old_writers_fenced",
            "waiting_window",
            "active_v2",
        }:
            return [1, current_state, current_generation, str(now_sec)]
        if not exists or current_state != "preparing":
            return [0, current_state, current_generation, str(now_sec)]
        window = int(window_seconds)
        margin = int(safety_margin)
        write_fields(
            current_generation,
            "old_writers_fenced",
            str(now_sec),
            str(now_sec + window + margin),
            min_writer,
        )
        return [1, "old_writers_fenced", current_generation, str(now_sec)]
    if action == "wait":
        if exists and current_state in {"waiting_window", "active_v2"}:
            return [1, current_state, current_generation, str(now_sec)]
        if not exists or current_state != "old_writers_fenced":
            return [0, current_state, current_generation, str(now_sec)]
        fence_time = int(field("fence_time"))
        if now_sec < fence_time:
            return [-5, current_state, current_generation, str(now_sec)]
        write_fields(
            current_generation,
            "waiting_window",
            field("fence_time"),
            field("not_before"),
            field("minimum_writer_version"),
        )
        return [1, "waiting_window", current_generation, str(now_sec)]
    if action == "activate":
        if exists and current_state == "active_v2":
            return [1, current_state, current_generation, str(now_sec)]
        if not exists or current_state != "waiting_window":
            return [0, current_state, current_generation, str(now_sec)]
        fence_time = int(field("fence_time"))
        not_before = int(field("not_before"))
        if now_sec < fence_time:
            return [-5, current_state, current_generation, str(now_sec)]
        if now_sec < not_before:
            return [-3, current_state, current_generation, str(now_sec)]
        write_fields(
            current_generation,
            "active_v2",
            field("fence_time"),
            field("not_before"),
            target_writer,
        )
        return [1, "active_v2", current_generation, str(now_sec)]
    if action == "abort":
        generation = current_generation if exists else "1"
        write_fields(
            generation,
            "aborted_closed",
            field("fence_time"),
            field("not_before"),
            min_writer,
        )
        return [1, "aborted_closed", generation, str(now_sec)]
    if action == "rollback_prepare":
        if exists and current_state in {
            "preparing",
            "old_writers_fenced",
            "waiting_window",
        }:
            return [1, current_state, current_generation, str(now_sec)]
        if not exists or current_state != "active_v2":
            return [0, current_state, current_generation, str(now_sec)]
        generation = str(int(current_generation) + 1)
        write_fields(generation, "preparing", "", "", min_writer)
        return [1, "preparing", generation, str(now_sec)]
    if action == "rollback_finish":
        if (
            exists
            and current_state == "preparing"
            and field("target_writer_version") == target_writer
        ):
            return [1, current_state, current_generation, str(now_sec)]
        if not exists or current_state != "waiting_window":
            return [0, current_state, current_generation, str(now_sec)]
        fence_time = int(field("fence_time"))
        not_before = int(field("not_before"))
        if now_sec < fence_time:
            return [-5, current_state, current_generation, str(now_sec)]
        if now_sec < not_before:
            return [-3, current_state, current_generation, str(now_sec)]
        write_fields(
            current_generation,
            "preparing",
            field("fence_time"),
            field("not_before"),
            target_writer,
        )
        return [1, "preparing", current_generation, str(now_sec)]
    return [-4, current_state, current_generation, str(now_sec)]
