# Production Control Hardening Diagrams

These Mermaid sources keep the same abstraction level so reviewers can compare the
privilege and artifact boundaries directly:

- [Before: checkout-owned root execution](production-control-and-artifact-binding-before.mmd)
- [Option 1: root-owned full release tree](production-control-and-artifact-binding-full-root-release-tree-after.mmd)
- [Option 2: commit-keyed full tracked-tree snapshot](production-control-and-artifact-binding-versioned-control-snapshot-after.mmd)
- [Option 3: privileged release agent](production-control-and-artifact-binding-privileged-release-agent-after.mmd)

The diagrams are design aids, not deployment evidence. In particular, the Ops VM,
internal Git mirror, Registry, release-evidence store, and current-selected commit
snapshots are shown as the approved target architecture; this task did not create or
inspect them.
