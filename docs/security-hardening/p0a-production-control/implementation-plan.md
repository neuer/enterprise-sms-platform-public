# P0-A Production Control Implementation Plan

The implementation-ready handoff for the selected KISS design is:

- [Commit-keyed full tracked-tree snapshot plan](implementation/versioned-control-snapshot.md)

It is anchored to source revision
`645e95e2691412121a5e3ffc750ad4e5bb071fd9` and the evidence collection recorded in
[context.md](context.md). The plan is design and implementation guidance only. It does not
record successful tests, authorize a production OS change, or claim that the Ops VM,
internal Git mirror, private Registry, release-evidence store, stable launcher, or control
snapshot has been deployed.

The selected boundary is intentionally small: reuse the existing Git/Registry/evidence
channels and release driver/lock/state machine; add a root-owned manager, one complete
Git-tracked-tree generation per approved commit, a root-owned commit approval marker, a
single relative `current` symlink, and a stable launcher. The marker grants authority but
does not select active state; `current` alone does that. No control digest, manifest-schema
change, parallel host-control state, or installer state machine is part of the plan.
