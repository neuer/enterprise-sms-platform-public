# Security Hardening Review: P0-A Production Control Boundary

## Evidence Basis

This review combines a fixed-revision source inspection at
`645e95e2691412121a5e3ffc750ad4e5bb071fd9` with the approved V1 production
architecture. The source collection is integrity-recorded in [context.md](context.md).
The worktree had in-progress release/control changes, so we read the fixed revision from
Git objects and do not present those changes as implemented evidence.

The observed structural issue is an ownership inversion: the current operator checkout
supplies shell, Python, Compose, or systemd paths that later execute with root/Docker
authority. The approved target already provides the right supply-chain anchors—internal
Git mirror, private Registry, release-evidence store, exact commit, and four
RepoDigests—but it needs an equally explicit root authorization and immutable execution
boundary for the same commit.

No production or pre-production host was accessed. This portfolio is a design artifact,
not a vulnerability closure, deployment record, benchmark, or test result.

## Constraints

We use a KISS profile:

- production remains one VM containing Core, PostgreSQL, and three isolated Redis
  instances; the common failure domain is accepted and must not be called HA;
- an Ops VM hosts the internal Git mirror, private Registry, release evidence,
  monitoring, redacted logs, and encrypted backup storage;
- GitHub Release Gate promotes through a controlled bridge; production jointly verifies
  exact Git commit, `current -> versions/<commit>`, and four image RepoDigests;
- a root-owned approval marker authorizes each snapshot commit; host-control adoption is
  an independent OS change even when the release driver orchestrates its commands;
- the existing driver, lifecycle lock, ReleaseStore, manifest schema, and forward-only
  migration/recovery behavior remain authoritative;
- no fifth image, OCI control artifact, privileged release daemon, new signing schema,
  Kubernetes/HA, or expanded offline path is introduced;
- the existing offline reader/package remains compatibility-only.

Older Phase 0 runbooks that still call Registry promotion a future exit target are
superseded for this V1 decision. They remain historical compatibility material, not the
normal release authority described here.

## Opportunity Portfolio

| Opportunity | Evidence | Options | Recommendation | Proposal |
| --- | --- | --- | --- | --- |
| Bind production root control and runtime artifacts to one release identity | Checkout-root execution and operator Git transition (`E001`, `E002`); exact forward-only release state (`E003`); RepoDigest/offline-exit decisions (`E004`); approved V1/KISS target (`E006`, `E007`) | 1. Root-owned full release tree; 2. Commit-keyed full tracked-tree snapshot; 3. Privileged release agent/control artifact | Option 2 under current single-host and KISS constraints | [Production control and artifact binding](proposals/production-control-and-artifact-binding.md) |

## Recommendation Summary

I recommend a stable root-owned launcher plus versioned, root-owned
production-control snapshots. Each `versions/<40hex commit>` generation contains the
complete Git-tracked tree and a canonical manifest of commit, tree, path, Git mode, byte
length, and SHA-256. The relative `current` symlink is the only active host-control fact:
a complete generation is prepared and fsynced first, then `current` is atomically
replaced. The root-owned approval marker is an authorization whitelist, not a second
active state; it is a root:root `0444` single-link file containing exactly the 41 ASCII
bytes `<commit>\n`. There is no snapshot intent, journal, installer state machine, or
daemon.

Ordinary release remains familiar. The operator fetches the exact commit from the
internal mirror; production pulls four images by RepoDigest using a read-only Registry
identity; existing release evidence is verified; and prepare/activate/status continue
through the existing lifecycle lock and state machine. Root never imports or executes
operator-checkout bytes. The release driver may prepare the commit snapshot, but it may
activate it only when the root-owned approval marker already exists; then ordinary
release prepare/activate/status verifies the same exact commit without adding a control
digest or changing the signed manifest schema.

This option is less invasive than replacing the operator checkout with one OS-owned
active tree and materially simpler than a new privileged agent or fifth artifact. Copying
the complete tracked tree (16.93 MiB at the fixed revision) avoids a selective-closure
allowlist and its hidden-import failure mode. The important residual risk moves to
entry-point enforcement: every root systemd entry, Python process, Compose file, and
bind-executed script must resolve inside the pinned snapshot.

## Next Decisions

- Approve the complete tracked-tree manifest contract, bounded file/byte limits, and the
  list of production root systemd services; development/test-only units should be absent
  or masked rather than invoked.
- Freeze the root-owned approval-directory and marker metadata contract. The normal
  wrapper and release manager must never create or repair approval markers.
- Review the OS precreation and owner/mode preview for
  `/etc/sms-platform/platform.env`, `/etc/sms-platform/secrets`, and
  `/var/lib/sms-platform/security-report` before any adoption request.
- Freeze bridge writer identities, production read-only identities, Registry immutability,
  evidence retention, and encrypted-backup key separation on the Ops VM.
- Define the two-person OS change approval that creates one commit marker and permits the
  driver-orchestrated `host-control prepare/activate/status` sequence.
- Implement and validate the selected plan in pre-production before requesting any
  production OS change.

The implementation handoff is available at
[implementation/versioned-control-snapshot.md](implementation/versioned-control-snapshot.md).
