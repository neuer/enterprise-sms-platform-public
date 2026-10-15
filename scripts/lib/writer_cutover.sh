#!/usr/bin/env bash
# 共用正式协调入口；调用方提供 compose()，不在这里修改权威状态。
activate_writer_cutover() {
  local attempt result
  for attempt in $(seq 1 16); do
    if result="$(compose test-update writer-cutover)"; then
      printf '%s\n' "$result" | python3 -c '
import json, sys
result = json.load(sys.stdin)
assert result["ok"] is True
assert result["state"] == "active_v2"
assert result["admission_state"] == "open"
assert result["error"] == ""
' || return 1
      printf '%s\n' "$result"
      return 0
    fi
    printf '%s\n' "$result" | python3 -c '
import json, sys
result = json.load(sys.stdin)
assert result["state"] == "waiting_window"
assert result["error"] == "waiting for not_before"
assert result["admission_state"] == "closed"
' || return 1
    if (( attempt < 16 )); then
      sleep 5
    fi
  done
  echo "writer cutover 窗口等待超时" >&2
  return 1
}
