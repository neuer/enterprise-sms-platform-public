from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "deploy/scripts"))

from release_store import ReleaseState, ReleaseStore, ReleaseStoreError  # noqa: E402

ALLOWED = {
    ReleaseState.STAGED: {ReleaseState.PREPARED, ReleaseState.FAILED},
    ReleaseState.PREPARED: {ReleaseState.ACTIVATING, ReleaseState.FAILED},
    ReleaseState.ACTIVATING: {
        ReleaseState.SUCCEEDED,
        ReleaseState.ROLLING_BACK,
        ReleaseState.RECOVERY_REQUIRED,
    },
    ReleaseState.ROLLING_BACK: {
        ReleaseState.ROLLED_BACK,
        ReleaseState.RECOVERY_REQUIRED,
    },
    ReleaseState.SUCCEEDED: set(),
    ReleaseState.ROLLED_BACK: set(),
    ReleaseState.FAILED: set(),
    ReleaseState.RECOVERY_REQUIRED: set(),
}


@pytest.mark.property
@given(
    expected=st.sampled_from(tuple(ReleaseState)),
    target=st.sampled_from(tuple(ReleaseState)),
)
@settings(max_examples=len(ReleaseState) ** 2, deadline=None)
def test_release_state_machine_accepts_exactly_declared_edges(
    expected: ReleaseState,
    target: ReleaseState,
) -> None:
    """任意状态对都必须与固定邻接表完全一致，禁止扩大终态或跳跃迁移。"""

    with tempfile.TemporaryDirectory(prefix="release-state-property-") as directory:
        store = ReleaseStore(
            Path(directory) / "releases",
            f"release-{expected.value}-{target.value}",
        )
        store.create(b'{"release_id":"property"}\n')
        state_path = store.release_dir / "state.json"
        state = store.read_state()
        state["state"] = expected.value
        state_path.write_text(
            json.dumps(state, separators=(",", ":"), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        state_path.chmod(0o600)

        if target in ALLOWED[expected]:
            store.transition(expected, target)
            assert store.read_state()["state"] == target.value
        else:
            with pytest.raises(ReleaseStoreError, match="illegal state transition"):
                store.transition(expected, target)
            assert store.read_state()["state"] == expected.value
