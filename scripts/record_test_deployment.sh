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

command -v gh >/dev/null 2>&1 || {
  echo "test-deployment: GitHub CLI unavailable" >&2
  exit 1
}
gh auth status --hostname github.com >/dev/null 2>&1 || {
  echo "test-deployment: GitHub CLI authentication is invalid" >&2
  exit 1
}
authenticated_repository="$(
  gh api "repos/$repository" --jq .full_name 2>/dev/null
)" || {
  echo "test-deployment: GitHub repository authentication preflight failed" >&2
  exit 1
}
authenticated_repository="$(
  printf '%s' "$authenticated_repository" |
    tr '[:upper:]' '[:lower:]'
)"
repository="$(
  printf '%s' "$repository" |
    tr '[:upper:]' '[:lower:]'
)"
[[ "$authenticated_repository" == "$repository" ]] || {
  echo "test-deployment: GitHub authentication context does not match repository" >&2
  exit 1
}

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
