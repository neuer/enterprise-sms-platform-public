#!/usr/bin/env bash
set -euo pipefail

[[ $# -eq 4 ]] || {
  echo "usage: scripts/record_test_deployment.sh OWNER/REPO COMMIT REF DESCRIPTION" >&2
  exit 2
}
repository="$1"
commit="$2"
source_ref="$3"
description="$4"
[[ "$repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || exit 2
[[ "$commit" =~ ^[0-9a-f]{40}$ ]] || exit 2
[[ "$source_ref" =~ ^origin/[A-Za-z0-9._/-]+$ && "$source_ref" != *".."* ]] || exit 2
[[ ${#description} -le 140 ]] || exit 2

deployment="$(
  python3 -c 'import json,sys
print(json.dumps({
 "ref":sys.argv[1],
 "environment":"test",
 "description":sys.argv[2],
 "auto_merge":False,
 "required_contexts":[],
 "transient_environment":False,
 "production_environment":False,
},separators=(",",":")))' \
    "$commit" "$description" |
    gh api --method POST \
      "repos/$repository/deployments" \
      --input -
)"
deployment_id="$(
  python3 -c 'import json,sys
value=json.load(sys.stdin); identifier=value.get("id")
assert isinstance(identifier,int) and identifier>0
print(identifier)' <<<"$deployment"
)"
python3 -c 'import json,sys
print(json.dumps({
 "state":"success",
 "environment":"test",
 "description":sys.argv[1],
},separators=(",",":")))' "$description" |
  gh api --method POST \
    "repos/$repository/deployments/$deployment_id/statuses" \
    --input - >/dev/null

echo "test-deployment: recorded id=$deployment_id commit=$commit ref=$source_ref"
