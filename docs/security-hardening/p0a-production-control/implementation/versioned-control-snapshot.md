# Implementation Plan: Commit-Keyed Full Tracked-Tree Snapshot

## Selected Design And Constraints

The selected P0-A design keeps the existing internal Git mirror, private Registry,
release-evidence store, remote driver, lifecycle lock, ReleaseStore, and signed release
manifest. It adds a small OS-owned execution boundary:

```text
/etc/sms-platform/production-control-approved/<40hex commit>  # authorizes
/usr/local/libexec/sms-platform/production-control/
  versions/<40hex commit>/                                    # complete tracked tree
  current -> versions/<40hex commit>                          # selects active
/usr/local/sbin/sms-compose                                   # stable launcher
```

Each generation contains every regular Git-tracked file from the exact commit, plus a
canonical manifest with commit, tree, sorted path, Git mode, byte length, and SHA-256.
There is no selective control closure and no separately computed control digest.

The approval marker and `current` have deliberately different meanings. A root-owned
marker says that an independent OS change authorized one commit. It never says that the
commit is active. The relative `current` symlink is the only active host-control fact. No
host state file, intent, journal, installer state machine, daemon, or ReleaseStore field is
added.

Registry production delivery still ends in the existing release prepare/activate/status
sequence for exact commit plus four RepoDigests. The driver first orchestrates the
separately authorized host-control prepare/activate/status sequence for that same commit.
The signed manifest schema does not change. Offline delivery remains compatibility-only
and does not advance host control.

This implementation plan does not authorize or perform a production or pre-production
change. A later production adoption requires a separate OS change approval and a separate
application release approval.

## Source Revision And Drift Check

- Fixed source revision: `645e95e2691412121a5e3ffc750ad4e5bb071fd9`
- Evidence collection SHA-256:
  `903df5c9feeb085a4d940472c358bf0ab56427f96e11208f129ba629d31ccbd6`
- Source drift at analysis time: `present`
- Fixed-revision tracked-tree baseline: 1,000 files, 17,750,771 bytes (16.93 MiB)

The shared worktree contains an in-progress P0-A implementation. We can use its selected
interfaces to make this handoff concrete, but it is not a refreshed implementation
revision, test result, or deployment record. Before coding acceptance, record a new exact
revision, recompute the tracked inventory, and re-review any drift in launcher, snapshot,
Git, release, recovery, Compose, systemd, env, secrets, or security-report boundaries.

If relevant drift changes the commit-keyed tree, marker authorization, single-`current`
fact, ordinary manifest schema, or forward-only recovery model, return to design review
instead of adapting silently.

## Affected Components

| Component | Required implementation responsibility |
| --- | --- |
| `deploy/scripts/production_control_snapshot.py` | Read the exact commit through fixed Git argv/environment, verify the root marker, build/verify the complete commit generation, and atomically activate `current`. |
| `deploy/production-sms-compose-launcher` | Sanitize execution, take the lifecycle lock, resolve/verify one marker-authorized `current` commit, pin its physical wrapper, and pass the verified lock. |
| `deploy/scripts/install_production_control_bootstrap.py` | Idempotently install manager, use a pre-existing approval marker, prepare/activate/status the initial snapshot, and atomically replace the launcher last. |
| `deploy/sms-compose` | Separate immutable `CONTROL_ROOT` from operator/data root, expose only host-control plan/prepare/activate/status, and expose no approve operation. |
| `scripts/deploy_release_remote.py` | For production Registry releases only, order host-control prepare before operator fast-forward, host-control activate/status after it, and then the unchanged release lifecycle. |
| `deploy/scripts/release_manager.py` | Preserve exact commit/four-image/evidence/migration gates and use the fixed platform env plus snapshot Compose/control paths without adding a control digest. |
| `deploy/scripts/release_store.py` | Preserve existing states, atomic writes, `recovery_required`, compensation, and forward-only recovery; add no host-control field or state. |
| `deploy/docker-compose*.yml` and host helpers | Resolve immutable Compose/scripts from the snapshot and mutable env/secrets/report data from fixed OS paths. |
| `deploy/systemd/*` | Route production root repository execution through the stable launcher; remove direct checkout imports/execs; keep test-only services absent or masked. |
| OS baseline/runbook | Precreate approval, platform env, secrets, and security-report paths; preview exact owner/mode changes and create markers outside the normal wrapper. |
| Ops VM/bridge | Retain internal Git, four-image Registry promotion, release evidence, monitoring, redacted logs, and encrypted backups with split identities. |
| Tests and runbooks | Prove full-tree equality, marker authorization, atomic current, launcher-last bootstrap, fixed path metadata, driver ordering, supply-chain mismatch, and forward-only recovery. |

## Ordered Work Packages

### WP1 — Freeze the exact snapshot and approval contracts

Define constants and fail-closed schemas before wiring any mutation:

- control root is fixed to
  `/usr/local/libexec/sms-platform/production-control`;
- generation name is exactly the lowercase 40-hex commit and the path is exactly
  `versions/<commit>`;
- `current` is a root-owned relative symlink whose target matches that exact form;
- snapshot directory and all descendant directories are root:root `0555`;
- regular snapshot files are root:root `0444` or `0555` according to Git mode `100644` or
  `100755`; links, submodules, devices, sockets, and other modes are rejected;
- manifest fields are exactly `schema_version`, `commit`, `tree`, and `files`; each file
  entry is exactly `path`, `mode`, `size`, and `sha256` in sorted path order;
- hard bounds cover file count, path length, per-file bytes, manifest bytes, tree-listing
  bytes, and total bytes;
- approval root is fixed to
  `/etc/sms-platform/production-control-approved`, root:root `0755`, with no writable or
  linked ancestor;
- each marker is a regular, single-link, root:root `0444` file named by one exact lowercase
  40-hex commit and containing exactly the 41 ASCII bytes `<commit>\n`.

The marker is authorization only. No `active`, `pending`, `accepted`, `previous`, or
transaction fields are stored there. The runtime code has read/validate behavior only.

Acceptance for WP1 is a machine-readable contract test that rejects any extra field,
path, mode, marker form, or generation form. The source tree at the fixed revision must
produce exactly 1,000 entries and 17,750,771 bytes; later revisions record their own
bounded values.

### WP2 — Make root approval independent and non-bypassable

Add one marker validator shared by snapshot prepare, activate, status, and the stable
launcher. It must:

- derive the marker name only from the already validated commit, never a caller-supplied
  path;
- open the approval directory and marker with no-follow descriptors;
- compare `lstat` and `fstat` device/inode/owner/group/mode/link-count metadata around the
  read;
- reject group/other-writable ancestors, symlinks, hardlinks, replacement races, wrong
  owner/mode, a size other than 41, content other than exact ASCII `<commit>\n`, and
  missing markers;
- avoid printing the approved commit, local Git object IDs, or other release identities in
  generic failure output where existing redaction rules forbid them.

Marker creation is not exposed through `sms-compose`, release prepare, the snapshot
manager, or containers. The independent OS change mechanism must create a staged file
containing exact ASCII `<commit>\n` in the approval directory, set root:root `0444`, fsync
the file, publish without
silently replacing an existing mismatched inode, and fsync the directory. Its plan output
shows the commit and metadata change, never credentials, private addresses, or file
contents.

Marker removal is a separate OS cleanup action. It must refuse the commit selected by
`current`, any commit referenced by an active or `recovery_required` release, the
forensic/evidence retention set, and any separately approved descendant recovery
candidate. Retained inactive generations never become backward activation targets.

### WP3 — Build a complete snapshot from fixed Git objects

Snapshot `prepare --expected-commit C` performs no `current` mutation. Under the existing
lifecycle lock it must:

- require marker `C` before reading candidate bytes;
- run `/usr/bin/git` without shell interpolation, as the fixed production operator, with
  system/global config disabled, replacement objects disabled, hooks disabled, fsmonitor
  and untracked cache disabled, no supplemental groups, and a fixed PATH/locale;
- verify `C` is a commit and read its tree/inventory through NUL-delimited plumbing;
- reject unsafe paths, `.git`, non-blob modes, duplicate/out-of-order entries, and any
  inventory exceeding the frozen bounds;
- read every blob by its object ID, require the advertised size, and compute SHA-256;
- create a same-filesystem root-owned staging tree with normalized read-only modes;
- write the canonical manifest, fsync every file and directory, publish
  `versions/C` atomically, and fsync `versions`;
- verify every expected file and directory and reject any extra inode before returning;
- if an exact complete `versions/C` already exists, verify and reuse it without rewriting.

Prepare returns strict JSON containing only status, commit, Git tree identity, file count,
byte count, and `current_target` (null for prepare). It does not calculate or persist a
control digest and does not create the marker.

### WP4 — Make `current` the single atomic active fact

Snapshot `activate --expected-commit C` runs under the existing lifecycle lock and:

- revalidates marker `C`, the complete `versions/C` snapshot, and its manifest;
- verifies the operator checkout is clean and its HEAD/commit/tree match `C` using the
  fixed Git read boundary;
- if `current` already names another commit, requires that commit to be a Git ancestor of
  `C`; backward and divergent transitions fail even when both markers are valid;
- verifies no conflicting host-control or application lifecycle operation is running;
- creates a temporary relative symlink in the control root;
- atomically replaces `current`, fsyncs the control root, resolves it again, and revalidates
  the physical generation;
- returns strict JSON with `current_target` exactly `versions/C`.

Snapshot `status` derives the active commit only from `current`. It then revalidates that
commit's root marker, manifest, complete tree, and clean operator checkout. It accepts no
caller-provided commit and never repairs drift.

An interruption before `os.replace` leaves the old complete target authoritative. An
interruption after `os.replace` leaves the new complete target authoritative. A retry
verifies the selected target and returns the same result. There is no intent, journal, or
accepted-state file.

### WP5 — Pin privileged execution and bootstrap launcher-last

The stable launcher is a root-owned regular `0555` file, not a symlink. It must:

- reject caller overrides of production project, control, Compose, Docker, secrets,
  lifecycle, and report paths;
- sanitize `BASH_ENV`, `ENV`, `PYTHONHOME`, `PYTHONPATH`, `PYTHONSTARTUP`,
  `PYTHONUSERBASE`, user-site loading, HOME, locale, and PATH;
- validate the lifecycle lock inode and acquire it before resolving `current`;
- validate the active marker and snapshot, pin the physical wrapper path, and pass only a
  verified inherited lock descriptor/environment;
- treat `__locked` as a private continuation only: it requires the verified held lock FD,
  is absent from public operator usage/sudoers, and fails before side effects on any
  direct or forged production invocation;
- expose no direct recovery selector: a damaged `current` is repaired only by a separately
  approved OS change, and an application rollback never selects an older control snapshot.

First bootstrap is an independent OS change and requires its commit marker to exist
before apply. The fixed root-owned bootstrap command verifies its own reviewed blob,
installs/verifies the snapshot manager, calls prepare/activate/status for the initial
commit, and atomically replaces `/usr/local/sbin/sms-compose` with the stable launcher
last. The parent directory is fsynced.

Before the last replacement, the legacy launcher remains authoritative. After it, manager,
marker, snapshot, and `current` are already complete. Re-running the same bootstrap
verifies/reuses those assets and completes or confirms the launcher replacement. It does
not create a bootstrap marker, intent, or state file.

### WP6 — Move mutable host data to fixed OS authorities

The OS baseline must preview, then precreate or deliberately migrate the following exact
metadata before launcher adoption:

| Path | Required owner:group | Required mode | Apply boundary |
| --- | --- | --- | --- |
| `/etc/sms-platform` | `root:root` | `0755`, no group/other write | Reject linked/unsafe parent; no recursive repair |
| `/etc/sms-platform/platform.env` | `root:root` | regular single-link `0600` | Copy values only in the approved OS secret/config procedure, never from release staging |
| `/etc/sms-platform/secrets` | `root:root` | directory `0700` | Require exact canonical inventory |
| each canonical secret | `root:root` | regular `0600` | No content in plan/log; validate nonempty and bounded through the existing secret preflight |
| `/etc/sms-platform/production-control-approved` | `root:root` | directory `0755` | OS approval mechanism only |
| each commit marker | `root:root` | 41-byte regular single-link `0444` | Exact ASCII `<commit>\n`; no normal-wrapper mutation |
| `/var/lib/sms-platform/security-report` | `root:root` | directory `0755` | Precreate outside Git |
| `.../control` | `root:root` | directory `0755` | Fixed control parent |
| `.../control/incoming` | `root:10001` | directory `0750` | Collector write/runtime read contract |
| `.../control/incoming/<date>.json` | `root:10001` | regular single-link `0640` | Runtime reader cannot truncate or replace host evidence |
| `.../control/requests` and `.../control/results` | `10001:10001` | directory `0700` | Runtime mailbox contract |
| `.../config` | `10001:10001` | directory `0700` | Runtime config contract |
| `.../nginx` | `101:101` | directory `0750` | Nginx log ownership contract |

Plan output must show `path`, current owner/group/mode/type, target
owner/group/mode/type, and action (`unchanged`, `create`, or explicit `migrate`). It must
not show environment values, secret names beyond the canonical inventory, report content,
credentials, or private host details. Apply refuses unknown numeric IDs, symlinks,
hardlinks, extra inventory, unsafe ancestors, and any unexpected existing inode. It never
uses an unbounded recursive chown/chmod.

Production Compose receives `/etc/sms-platform/platform.env` through the fixed internal
env-file interpolation. Runtime secret preparation receives
`/etc/sms-platform/secrets`. Security-report collectors and containers use only the fixed
`/var/lib` subdirectories. Development/test defaults may retain checkout-relative paths,
but production mode must fail closed on them.

### WP7 — Preserve the ordinary release schema and state machine

Do not add a control digest or snapshot field to the signed manifest, release evidence
schema, ReleaseStore state, or events. Ordinary release prepare/activate/status continues
to enforce:

- exact manifest commit and clean operator Git identity;
- release evidence for that exact commit;
- exactly four immutable image RepoDigests and runtime image readback;
- migration head/direction and forward-only schema behavior;
- existing lifecycle states, compensation, rollback, and terminal `recovery_required`.

The stable launcher and host-control status already prove that the pinned snapshot is
`versions/<same manifest commit>`. Release code can use the pinned physical control root
for Compose and Python while using the operator project root only for explicitly bounded
Git identity/data. It must never re-import checkout code to “double check” the commit.

### WP8 — Order the production Registry driver without hiding the OS change

For production Registry/RepoDigest releases, the deterministic driver plan is:

1. validate local manifest, exact approved remote ref, four RepoDigests, and release
   evidence;
2. fetch the exact commit on the production operator checkout;
3. call host-control prepare for that exact commit; missing root marker blocks;
4. preserve the existing Git rollback ref for application evidence only, then
   fast-forward the clean checkout; the ref never authorizes host-control activation;
5. call host-control activate and status; both revalidate marker and exact commit;
6. call the existing release prepare, activate, and status;
7. perform existing public/status probes and bounded cleanup.

The host-control calls remain visibly labeled OS adoption steps in plan/dry-run and change
records even though the driver orchestrates them. Execution approval must cover the root
marker and those calls; normal application approval alone is insufficient.

The driver validates strict host-control result fields: `status`, `commit`, `tree`,
`files`, `bytes`, and `current_target`. It stops before application side effects on any
field, output, command, or identity mismatch. A retry may reuse the prepared complete
snapshot and retained release staging.

Offline schema-v2 compatibility bypasses host-control advancement. It must not create a
marker, activate a new snapshot, or automatically substitute for a Registry outage.

### WP9 — Close every privileged entry and fixed interpreter path

Inventory production root systemd `ExecStart*`, `ExecStop*`, `ExecReload*`, helper
interpreters, Compose includes, local Python imports, shell sources, and bind-executed
scripts. Route repository behavior through `/usr/local/sbin/sms-compose` so the launcher
pins the snapshot. Fixed non-repository OS binaries may remain direct when their paths and
arguments are independently trusted.

Backup, restore drill, lifecycle status, and security-report collector units should enter
the stable wrapper and then use snapshot code plus fixed OS data paths. Development-only
vendor/test agents must be absent or explicitly masked in production rather than copied
into the production root TCB.

Add a source/contract test that fails when a production root unit references
`/opt/sms-platform`, a checkout Python virtualenv, a checkout Compose file, or another
operator-writable executable path.

### WP10 — Establish Ops VM dual-channel and validation evidence

The bridge is the normal writer to the internal Git mirror, four-image Registry
namespaces, and append-only release evidence. Production uses distinct read-only
identities. Promotion records exact commit plus four RepoDigests; the root marker remains
an OS approval artifact and is not uploaded as a fifth Registry artifact or added to the
signed manifest.

Monitoring and logs must redact credentials, phone data, private addresses, and raw
approval/release command output. Backups arrive encrypted; decryption keys remain in a
separately controlled escrow rather than on the Ops VM storage surface.

Complete pre-production tamper, crash, resource, release, backup, and restore exercises
before drafting a production change. Preserve observed, inferred, and proposed labels in
the evidence package.

## Compatibility And Migration

The first OS bootstrap is the only compatibility bridge from the legacy checkout symlink
to the stable launcher. It is launcher-last, idempotent, and uses a pre-existing approved
initial commit marker. Once the stable launcher is active, no production root action may
fall back to the checkout.

The complete snapshot includes application source as well as deployment/control files,
but containers still run the four promoted images. The extra source bytes are an accepted
bounded Phase 0 simplification to avoid a selective closure; they do not authorize local
builds.

The ordinary signed release manifest, release evidence fields, ReleaseStore states,
database schema, and four-image names remain compatible. No migration rewrites historical
release records. Old snapshot generations and markers are retained according to the
recovery/evidence floor and removed only by a separate OS cleanup.

Fixed OS env/secrets/report migration must complete before launcher adoption. The change
preview lists metadata and exact actions without content. Containers and application
release must not create these roots as a convenience fallback.

Historical offline packages remain readable for compatibility/audit. New normal release
uses internal Git plus Registry/evidence; Registry outage blocks new release rather than
activating offline automatically.

Older Phase 0 runbooks that describe Registry as a future exit target are superseded for
this implementation decision. Their offline material is retained only as compatibility
history; the fixed platform env, secrets, approval marker, security-report tree, and
launcher-last bootstrap prerequisites in this plan must be satisfied before adoption.

## Tactical Protections During Migration

- Keep production changes blocked until root entry inventory and fixed OS path preview are
  reviewed; a partially migrated launcher is not acceptable.
- Require one explicit root marker per candidate commit before any host-control prepare;
  do not use a directory-wide wildcard or “latest” marker.
- Keep operator Git fetch/HEAD/status checks fail closed and use fixed Git configuration;
  root authorization does not make a dirty checkout safe.
- Stop affected root timers/services and take the existing lifecycle lock during bootstrap
  and snapshot activation.
- Retain the old launcher bytes for forensic comparison, plus the active snapshot, marker,
  release staging, and rollback ref until bootstrap/release status and reboot checks
  complete. Retention does not authorize reactivation of old control.
- Reject environment/path overrides rather than attempting to sanitize arbitrary values
  after they enter Compose, Python, Docker, or report code.
- Preserve current application state on Ops VM outage; do not use the offline path as an
  availability shortcut.
- If `recovery_required` exists, block marker/snapshot cleanup and ordinary host-control
  switching until the existing recovery process explicitly permits a compatible forward
  action.

## Tests And Security Validation

Unit and contract coverage must include:

- exact 40-hex generation and marker naming, canonical JSON, duplicate-key rejection, and
  bounded path/file/byte parsing;
- full Git tree equality, including tracked empty files and executable modes; rejection of
  submodules, symlinks, unsafe names, extra files, missing files, hardlinks, wrong
  owner/mode, tree drift, size drift, and SHA-256 drift;
- fixed Git argv/environment and fixed operator UID/GID with no shell, hooks, replacement
  objects, user config, fsmonitor, untracked cache, or supplemental groups;
- approval marker absent/wrong owner/wrong group/wrong mode/wrong size/wrong
  `<commit>\n` content/symlink/hardlink/race cases for prepare, activate, status, launcher,
  and bootstrap;
- proof that no normal wrapper action, release action, or container path creates a marker;
- prepare idempotence without `current` mutation;
- activate failure before atomic replacement retaining the old pointer and failure after
  replacement retaining the new complete pointer; idempotent retry of both outcomes;
- launcher lock acquisition before current resolution, physical path pinning, inherited
  lock inode verification, and hostile environment rejection;
- direct `__locked`, forged marker, unlocked descriptor, and wrong-inode descriptor
  rejection before Docker or filesystem side effects;
- bootstrap failure before/after manager installation, snapshot prepare, current switch,
  and launcher replacement, including a successful idempotent rerun;
- fixed platform env/secrets/report owner/mode/type validation and redacted change preview;
- root systemd source checks proving no production unit executes/imports checkout bytes;
- driver sequence and strict JSON validation for host-control prepare/activate/status
  before ordinary release prepare/activate/status;
- Registry release success/failure/retry and offline compatibility paths without host
  approval or snapshot advancement;
- exact commit, four RepoDigest, image ID/label, release evidence, migration, compensation,
  rollback, and `recovery_required` regression cases.

Pre-production security exercises must tamper with checkout files, `.git` refs/config,
approval markers, current link, snapshot manifest/files, fixed OS paths, Registry tags and
digests, release evidence, environment variables, and systemd units. Every mismatch must
block before the relevant side effect and leave enough redacted status for recovery.

Run product tests and repository checks only in the implementation task and record exact
commands/results there. This documentation task does not claim that any test has passed.

## Performance And Resource Benchmarks

The fixed source revision provides a reproducible starting workload: 1,000 tracked files
and 17,750,771 bytes. Refresh those values at the implementation revision and record:

| Workload | Metrics | Decision criterion |
| --- | --- | --- |
| Marker validation | wall time, syscalls, peak RSS | Bounded fixed-path check; no network/process hop |
| Snapshot plan | Git calls, inventory bytes, wall time, peak RSS | Read-only and within the approved OS planning window |
| First prepare | files/bytes hashed and copied, wall time, peak RSS, disk delta | Completes within measured OS maintenance window and fixed safety bounds |
| Idempotent prepare | wall time, bytes reread, pointer writes | Verifies existing generation and performs no `current` mutation |
| Activate/status | lock hold time, wall time, files/bytes verified, peak RSS | No mixed generation; completes within the frozen lifecycle window |
| Release prepare/status | Git/evidence/Registry calls, wall time, peak RSS | No application request-path process/hop; fits existing release window |
| Retention | active/old generation count and disk bytes | Meets recovery/evidence floor without exhausting the OS disk budget |
| Four-image pull/readback | transferred bytes/time and four RepoDigests | No mutable tag or local build used as authority |

Do not fabricate a numeric threshold. Freeze time, RSS, and retained-disk limits from a
representative pre-production measurement and approved maintenance/disk budgets. Snapshot
growth beyond the hard safety bound returns to design review rather than silently raising
limits.

## Rollout And Rollback

### Pre-production rollout

Use a dedicated OS change rehearsal:

- preview and precreate fixed env, secrets, approval, and security-report metadata;
- create the exact initial commit marker through the independent OS approval process;
- stop affected root timers/services and take the lifecycle lock;
- install/verify manager, prepare/activate/status the initial complete snapshot, and
  atomically replace the launcher last;
- rerun bootstrap to prove idempotence, then reboot and revalidate start gates;
- execute one Registry release at a new marker-authorized commit, one absent-marker
  rejection, one failed release before migration, one migration failure, one
  `recovery_required` path, and one Ops VM outage;
- complete backup and restore evidence without claiming HA.

### Production approval boundary

Production requires a new explicit approval after pre-production evidence. The OS change
record contains exact bootstrap/manager/launcher file identities, the initial or candidate
commit marker, snapshot commit/tree/file count/byte count, `current` before/after target,
fixed-path owner/mode preview, unit changes, old launcher bytes, retention targets, and
named reviewers. It contains no secret values, production addresses, credentials, or raw
private logs.

Every later Phase 0 release commit requires a lightweight OS approval that creates its
marker and authorizes the driver host-control sub-sequence. Application release approval
does not imply marker creation.

### Bootstrap failure recovery

- Before final launcher replacement, the legacy launcher remains authoritative. Correct
  complete prerequisites and rerun; there is no progress marker to interpret.
- After launcher replacement, do not restore a prior `current` target or return root
  execution to the checkout. Use the independently approved fixed bootstrap/OS recovery
  entry to publish a reviewed forward launcher fix and, when host-control bytes change,
  activate only a marker-authorized descendant commit.
- If fixed OS paths, marker, or both snapshots are unsafe, stop and repair through the OS
  change process. Never point root execution back to arbitrary checkout bytes.

### Host-control forward recovery

Once `current` exists, host-control activation permits only the same commit or a Git
descendant. Backward and divergent transitions fail even before the dependent application
release is PREPARED and even when both commits have valid markers. Repair therefore uses
an independently approved descendant commit; application images use existing
compensation, `recovery_required`, or an approved forward rollback candidate.

Removing a marker does not switch `current` and must not be used as rollback. If an active
marker disappears or drifts, status fails closed and the OS procedure restores that same
commit's exact root:root `0444`, 41-byte `<commit>\n` marker before any forward action.

### Application and Registry rollback

Never retag mutable names or delete RepoDigests to simulate rollback. Keep evidence and
promoted digests. Use the existing release manager's compensation/forward-rollback
contracts; schema remains forward-only. Ops VM outage blocks new release, not current
runtime and not automatic offline activation.

## Acceptance Criteria

The design is ready for a production OS change request only when current evidence proves:

- a marker-authorized exact commit produces one complete root-owned
  `versions/<commit>` tree whose manifest matches every regular tracked Git file;
- the fixed-revision complete-tree baseline is refreshed and remains within approved hard
  bounds and measured maintenance/disk budgets;
- prepare, activate, status, launcher, and bootstrap all reject absent or unsafe markers;
- operator, normal wrapper/release actions, and containers cannot create approval markers;
- `current` is always a safe relative link and is the only active host-control fact;
- same-commit activation is idempotent and every different activated commit is a verified
  Git descendant of the current commit; backward/divergent transitions fail closed;
- no host state, intent, journal, daemon, control digest, new manifest field, or installer
  state machine exists;
- no production root entry executes, imports, sources, or gives Compose authority to
  operator-checkout bytes;
- root env, canonical secrets, approval, and security-report paths are precreated at the
  exact reviewed owner/mode/type contract and application release cannot replace them;
- driver host-control prepare/activate/status is exact, fail closed, and precedes the
  unchanged release lifecycle for Registry production releases;
- exact internal-Git commit, current snapshot commit/tree, release evidence, four
  RepoDigests, and runtime image readback agree;
- the signed release schema and ReleaseStore states are unchanged, and all
  `recovery_required`/forward-only migration regressions pass;
- offline remains compatibility-only and cannot advance host control or auto-fallback;
- pre-production crash, tamper, reboot, resource, backup, restore, Ops outage, and
  application recovery evidence is reviewed;
- single production/Ops VM failure domains and lack of KMS/HSM remain explicitly accepted
  residual risks;
- production remains untouched until a separate OS approval and application release
  approval are granted.

## Open Decisions

- Name the two-person OS approvers and evidence-retention location for marker creation and
  cleanup.
- Freeze the retention floor for the active commit, `recovery_required` evidence, and
  forensic/audit needs; retained inactive snapshots are not rollback targets.
- Confirm production numeric identities `10001` and `101` against named accounts before
  accepting the fixed-path metadata preview.
- Freeze the final production root systemd unit allowlist and the set of test/development
  units that must be absent or masked.
- Record measured pre-production time, RSS, and disk thresholds at the refreshed
  implementation revision.
- Freeze bridge writer and production read-only identities for internal Git, Registry,
  and release evidence, plus encrypted-backup key custody.
