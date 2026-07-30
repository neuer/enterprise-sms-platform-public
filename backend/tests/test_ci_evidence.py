from __future__ import annotations

import sys
from pathlib import Path
from subprocess import CompletedProcess

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS))

import reuse_pr_ci_evidence as reuse_module  # noqa: E402
import verify_ci_commit as verify_module  # noqa: E402
from reuse_pr_ci_evidence import commit_tree, merged_pr_head, write_outputs  # noqa: E402
from verify_ci_commit import CiEvidenceError, ci_gate_status, github_token  # noqa: E402

COMMIT = "1" * 40
HEAD = "2" * 40
TREE = "3" * 40


def check_run(
    *,
    head_sha: str = COMMIT,
    status: str = "completed",
    conclusion: str | None = "success",
    app_id: int = 15368,
    app_slug: str = "github-actions",
) -> dict[str, object]:
    return {
        "id": 42,
        "name": "ci-gate",
        "head_sha": head_sha,
        "status": status,
        "conclusion": conclusion,
        "app": {"id": app_id, "slug": app_slug},
    }


def test_ci_gate_accepts_only_exact_success_from_github_actions() -> None:
    assert ci_gate_status({"check_runs": [check_run()]}, commit=COMMIT) == "success"
    assert (
        ci_gate_status(
            {"check_runs": [check_run(app_id=1, app_slug="third-party")]},
            commit=COMMIT,
        )
        == "missing"
    )
    assert (
        ci_gate_status(
            {"check_runs": [check_run(status="in_progress", conclusion=None)]},
            commit=COMMIT,
        )
        == "pending"
    )
    assert (
        ci_gate_status(
            {"check_runs": [check_run(conclusion="failure")]},
            commit=COMMIT,
        )
        == "failure"
    )


@pytest.mark.parametrize("document", [None, {}, {"check_runs": [None]}])
def test_ci_gate_rejects_malformed_api_documents(document: object) -> None:
    with pytest.raises(CiEvidenceError):
        ci_gate_status(document, commit=COMMIT)


def test_github_token_prefers_environment_without_invoking_gh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_run(*args: object, **kwargs: object) -> CompletedProcess[str]:
        raise AssertionError("gh must not be invoked when an environment token exists")

    monkeypatch.setattr(verify_module.subprocess, "run", unexpected_run)

    assert github_token({"GITHUB_TOKEN": "primary", "GH_TOKEN": "secondary"}) == "primary"
    assert github_token({"GH_TOKEN": "secondary"}) == "secondary"


def test_github_token_uses_native_gh_keyring_without_logging_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, object]] = []

    def fake_run(*args: object, **kwargs: object) -> CompletedProcess[str]:
        calls.append((args, kwargs))
        return CompletedProcess(args[0], 0, stdout="keyring-token\n", stderr="")

    monkeypatch.setattr(verify_module.subprocess, "run", fake_run)

    assert github_token({}) == "keyring-token"
    assert calls[0][0] == (["gh", "auth", "token", "--hostname", "github.com"],)
    assert calls[0][1]["capture_output"] is True


def test_github_token_returns_none_when_native_auth_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        verify_module.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(args[0], 1, stdout="", stderr="denied"),
    )

    assert github_token({}) is None


def test_test_deployment_recording_rechecks_native_auth_without_exporting_token() -> None:
    source = (SCRIPTS / "record_test_deployment.sh").read_text(encoding="utf-8")

    assert "gh auth status --hostname github.com" in source
    assert 'gh api "repos/$repository" --jq .full_name' in source
    assert source.index("gh auth status") < source.index('"repos/$repository/deployments"')
    assert "gh auth token" not in source
    assert "export GITHUB_TOKEN" not in source
    assert "export GH_TOKEN" not in source


def test_reuse_requires_unique_merged_main_pr_for_candidate() -> None:
    pull = {
        "number": 17,
        "state": "closed",
        "merged_at": "2026-07-30T00:00:00Z",
        "merge_commit_sha": COMMIT,
        "base": {"ref": "main"},
        "head": {
            "sha": HEAD,
            "repo": {"full_name": "example/repository"},
        },
    }
    assert merged_pr_head([pull], candidate=COMMIT) == HEAD
    assert (
        merged_pr_head(
            pull,
            candidate=COMMIT,
            repository="example/repository",
            expected_number=17,
            expected_head=HEAD,
        )
        == HEAD
    )
    assert merged_pr_head([], candidate=COMMIT) is None
    assert merged_pr_head([pull, pull], candidate=COMMIT) == HEAD
    other = {
        **pull,
        "head": {
            "sha": "4" * 40,
            "repo": {"full_name": "example/repository"},
        },
    }
    assert merged_pr_head([pull, other], candidate=COMMIT) is None
    assert (
        merged_pr_head(
            pull,
            candidate=COMMIT,
            repository="other/repository",
            expected_number=17,
            expected_head=HEAD,
        )
        is None
    )


def test_reuse_tree_and_outputs_are_strict(tmp_path: Path) -> None:
    assert commit_tree({"tree": {"sha": TREE}}) == TREE
    with pytest.raises(CiEvidenceError):
        commit_tree({"tree": {"sha": "short"}})

    output = tmp_path / "github-output"
    write_outputs(output, reuse=True, tested_sha=HEAD)
    assert output.read_text(encoding="utf-8") == (
        f"reuse=true\ntested_sha={HEAD}\n"
    )


def test_post_merge_reuse_binds_pr_head_tree_and_ci_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    pull = {
        "number": 17,
        "state": "closed",
        "merged_at": "2026-07-30T00:00:00Z",
        "merge_commit_sha": COMMIT,
        "base": {"ref": "main"},
        "head": {
            "sha": HEAD,
            "repo": {"full_name": "example/repository"},
        },
    }

    def fake_github_json(url: str, *, token: str | None) -> object:
        assert token == "workflow-token"
        calls.append(url)
        if url.endswith("/pulls/17"):
            return pull
        if url.endswith(f"/git/commits/{COMMIT}"):
            return {"tree": {"sha": TREE}}
        if url.endswith(f"/git/commits/{HEAD}"):
            return {"tree": {"sha": TREE}}
        if url.endswith(f"/commits/{HEAD}/check-runs?filter=latest&per_page=100"):
            return {"check_runs": [check_run(head_sha=HEAD)]}
        raise AssertionError(url)

    monkeypatch.setenv("GITHUB_TOKEN", "workflow-token")
    monkeypatch.setattr(reuse_module, "github_json", fake_github_json)
    output = tmp_path / "github-output"

    result = reuse_module.main(
        [
            "--repository",
            "example/repository",
            "--candidate",
            COMMIT,
            "--event-name",
            "post_merge",
            "--expected-head",
            HEAD,
            "--pr-number",
            "17",
            "--github-output",
            str(output),
        ]
    )

    assert result == 0
    assert output.read_text(encoding="utf-8") == (
        f"reuse=true\ntested_sha={HEAD}\n"
    )
    assert len(calls) == 4


def test_post_merge_evidence_mismatch_falls_back_to_full_ci(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        reuse_module,
        "github_json",
        lambda url, *, token: {
            "number": 17,
            "state": "closed",
            "merged_at": "2026-07-30T00:00:00Z",
            "merge_commit_sha": COMMIT,
            "base": {"ref": "main"},
            "head": {
                "sha": "4" * 40,
                "repo": {"full_name": "example/repository"},
            },
        },
    )
    output = tmp_path / "github-output"

    result = reuse_module.main(
        [
            "--repository",
            "example/repository",
            "--candidate",
            COMMIT,
            "--event-name",
            "post_merge",
            "--expected-head",
            HEAD,
            "--pr-number",
            "17",
            "--github-output",
            str(output),
        ]
    )

    assert result == 0
    assert output.read_text(encoding="utf-8") == "reuse=false\ntested_sha=\n"


def test_post_merge_reuse_rejects_partial_binding(tmp_path: Path) -> None:
    output = tmp_path / "github-output"

    result = reuse_module.main(
        [
            "--repository",
            "example/repository",
            "--candidate",
            COMMIT,
            "--event-name",
            "post_merge",
            "--expected-head",
            HEAD,
            "--github-output",
            str(output),
        ]
    )

    assert result == 2
    assert not output.exists()
