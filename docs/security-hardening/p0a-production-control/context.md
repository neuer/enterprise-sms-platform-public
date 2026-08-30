# P0-A Production Control Hardening Context

## Analysis identity

- Analysis ID: `hardening_20260830_p0a_production_control`
- Evidence mode: fixed-revision source review plus an approved target-architecture record
- Target revision: `645e95e2691412121a5e3ffc750ad4e5bb071fd9`
- Source drift: `present`
- Evidence collection SHA-256:
  `903df5c9feeb085a4d940472c358bf0ab56427f96e11208f129ba629d31ccbd6`
- Artifact count: 20 (19 repository files and one supplied architecture record)

The shared worktree contained in-progress release/control changes when this analysis was
written. We therefore inspected the target revision through immutable Git objects and did
not treat those working-tree changes as implemented evidence. This directory is a derived
design product; it is not a security scan seal, a production change record, or proof that
the proposed controls exist.

## Approved target architecture record

The following record is the final design and selected-implementation constraint supplied
for this analysis. Its SHA-256 is
`76ec6c52e494ab46eaf7fb694f0876aedc81e57ba61ce9aeca80f8ddf4782fb5`.

```text
V1_TARGET|
production=single-vm:postgresql+redis-broker+redis-auth+redis-control|
ops-vm=internal-git-mirror+private-registry+release-evidence+monitoring+logging+encrypted-backup|
delivery=github-release-gate>bridge>internal-git+registry+evidence>production|
binding=exact-git-commit+root-owned-full-git-tracked-tree-snapshot-at-same-commit+four-repodigests|
snapshot=versions/<40hex-commit>:path+git-mode+size+sha256+tree-identity;current-relative-symlink-only-active-fact|
authorization=/etc/sms-platform/production-control-approved/<commit>:root-owned-0444-41-byte-commit-newline-marker|
release=registry-host-control-prepare+activate+status-then-existing-release-prepare+activate+status;no-control-digest;no-manifest-schema-change|
host-control=driver-orchestrated-but-independent-os-adoption-or-bootstrap|
os-state=/etc/sms-platform/platform.env+/etc/sms-platform/secrets+/var/lib/sms-platform/security-report:precreated-with-owner-mode-preview|
offline=compatibility-only|
size=complete-tracked-tree-at-target-revision-16.93MiB-accepted|
kiss=no-selective-control-closure,no-fifth-image,no-oci-control-artifact,no-new-daemon,no-new-signing-schema,no-kubernetes,no-ha,no-offline-expansion
```

This record supersedes an earlier draft assumption that daily production release would
not use an internal Git mirror. The approved V1 design deliberately retains two mutually
checked delivery channels: source identity through the internal Git mirror, and runtime
artifact identity through the private Registry plus release-evidence store.

The selected KISS implementation snapshots every regular Git-tracked file, not a manually
maintained control closure. At the fixed revision the tree contains 1,000 tracked files
and 17,750,771 bytes (16.93 MiB), which the approved design accepts as a bounded Phase 0
cost. The root-owned approval marker authorizes a commit; it is not an active pointer,
transaction intent, or recovery journal. The relative `current` symlink remains the sole
fact selecting which complete snapshot is active.

> **V1 supersession notice:** older Phase 0 runbooks that call the private Registry a
> future exit target and describe the signed offline archive as the normal first-release
> path are historical for this proposal. The approved V1 normal path is now internal Git
> plus private Registry plus release evidence; offline is compatibility-only. The fixed
> platform-env, secrets, approval-marker, security-report, and launcher-last bootstrap
> prerequisites in this analysis govern P0-A adoption. This task does not rewrite those
> older runbooks.

## Evidence map

| Evidence | Reader-facing title | Classification | What it establishes |
| --- | --- | --- | --- |
| `E001` | Root execution currently resolves through the application checkout | Observed | `sms-compose`, Compose definitions, Python managers, and several systemd services can be loaded from `/opt/sms-platform`; the installed wrapper is a symlink to that tree. |
| `E002` | Production deployment mutates the operator checkout before privileged prepare | Observed | The remote driver performs operator-side Git fetch/fast-forward and then invokes privileged `release prepare`, `activate`, and `status`. |
| `E003` | Release identity and recovery are already exact and forward-only | Observed | The manifest binds an exact commit and four images; the release store has explicit terminal `recovery_required`; schema compensation does not perform downgrade. |
| `E004` | RepoDigest promotion is the intended post-offline supply-chain path | Observed | Existing decisions bind candidate scans and image identity to RepoDigest and explicitly describe the signed offline archive as temporary. |
| `E005` | Phase 0 deliberately accepts one production VM as a common failure domain | Observed | PostgreSQL, Core, and three isolated Redis instances share a VM; TLS, ACL, AOF, backup, and fail-closed behavior reduce but do not remove that common failure. |
| `E006` | V1 dual-channel target and joint release identity | Supplied/approved | Internal Git, private Registry, and release evidence are retained; `current -> versions/<commit>` binds the root snapshot to the same exact commit as four RepoDigests. |
| `E007` | KISS, root approval, and compatibility constraints | Supplied/approved | Every candidate commit needs a root-owned OS approval marker; the design reuses the driver, lock, and state machine and adds no selective closure, control digest, manifest field, fifth image, OCI control artifact, privileged daemon, signing schema, Kubernetes/HA, or expanded offline path. |

## Repository evidence inventory

The collection digest is the SHA-256 of the following canonical `sha256  path\n` lines in
table order, followed by
`76ec6c52e494ab46eaf7fb694f0876aedc81e57ba61ce9aeca80f8ddf4782fb5  supplied://approved-v1-target\n`.

| SHA-256 | Repository-relative path | Primary evidence IDs |
| --- | --- | --- |
| `de31eb84e187ea341eb0a1c2c143e9cb9374e08ee75583acbba97a5da04dc851` | `.github/workflows/release-gate.yml` | `E004` |
| `2ee822b36baacd9618bb4bea0558438563a6699ff37a4b39c793cf7e4cd81f31` | `scripts/verify_release.sh` | `E003`, `E004` |
| `d704974f33ecf6941c126faaf1b6038ff380737b606e43976b720ae6ef7c0d2e` | `scripts/create_release_manifest.py` | `E003`, `E004` |
| `582ea774ed583b088dca0a540250007dc2397a7b8a826afee0c47a2b380b71f1` | `scripts/deploy_release_remote.py` | `E002`, `E003` |
| `d28bede19c843691c5bb3eb9df101bc58c96c5170a669bf8f155dd128d78f290` | `deploy/sms-compose` | `E001`, `E003` |
| `dfc40cbcab966f17ee54df1dcd136107bda25fb28712f839cf2bcf640b89a463` | `deploy/scripts/release_manager.py` | `E001`, `E003` |
| `9916be03a53bd9b636ebeb5ecd18d39c49147ce59e2d4db0b852b35362a8c9b8` | `deploy/scripts/release_store.py` | `E003` |
| `214bc422267c7bb5c26811a4e96d140e88993ef23561b464e62fe51d4c507215` | `deploy/scripts/release_manifest.py` | `E003` |
| `ef5d1b142f7a285491efcfd14366dd52e9d022e2ac665226f2ffb87fc63d3849` | `deploy/scripts/install_production_host_assets.py` | `E001` |
| `94c5a634ab820401a50bb9a995de753555c87b7ac81073d3687cd21c1d94f06e` | `deploy/systemd/sms-platform.service` | `E001` |
| `af176d7e1bb3324ae3cc9e2b2a68e430d9c841910c4bbf2d832f98550ea2052a` | `deploy/systemd/sms-backup.service` | `E001` |
| `1ba878fab7e3bc001b286634be01c8c8791ce5ba9dfa0bbdae40d27e87263556` | `deploy/systemd/sms-lifecycle-status.service` | `E001` |
| `2d6d72648537b771aafb63da1380cd4f9626fbac5933bf709530e84a758dec30` | `deploy/systemd/vendor-control-agent.service` | `E001` |
| `660d2b960d1e1c96584a8ecec0ab44171376a912576d133294211d2dcee02d20` | `deploy/docker-compose.yml` | `E001`, `E005` |
| `308d03dbe9d3cadb937f1a2912a51507580fa82450f6449f0cdff632859bb472` | `docs/threat-model.md` | `E004`, `E005` |
| `760fb7acb6e1eca73ca4ed85306e049ffaf28063890fae83960ba76672958a0c` | `docs/runbooks/production-phase0-baseline.md` | `E004`, `E005` |
| `2f4b900abefe99dbd27f6deffd459dbedcb6f0217ec97c33a7758f72b22edd97` | `deploy/redis-ha.md` | `E005` |
| `fef4126289bb2e9aad31825cf6fb459ca0871d6df9bdc3f94515590eac21ab56` | `docs/DECISIONS.md` | `E003`, `E004`, `E005` |
| `e1f74b29bddb777fb5a7410fb61a1b3f2937f82623beedb34d463619f910e998` | `MAINTENANCE.md` | `E003`, `E004` |

## Claim discipline

- **Observed** means the property is present in the fixed revision or an explicit accepted
  risk is recorded there.
- **Inferred** means the source-backed behavior supports a structural conclusion, but the
  conclusion is not itself a runtime measurement.
- **Proposed** means the V1 target or this hardening design; it must not be described as
  deployed, tested, or production-accepted.

## Evidence limitations

- No production or pre-production host was accessed in this task.
- No Registry, internal Git mirror, bridge, evidence database, monitoring, logging, or
  backup service was provisioned or queried.
- No security scan seal, runtime owner/mode readback, benchmark, failure injection, or
  restore drill was supplied for this analysis.
- The selected in-progress P0-A implementation was inspected only to align this handoff
  with its commit-keyed full-tree snapshot interface. Its working-tree presence is not
  evidence that the change is complete, reviewed, tested, or deployed.
- The source review establishes design and code-path facts only. It does not prove that an
  operator can currently exercise the attack path on a real host, nor that the proposed
  design closes it until implementation and validation are complete.
