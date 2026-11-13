# Workspace Isolation

This reference describes Phase 2-C with its D1 resource extension. It is not permission to run isolation from an already isolated Codex environment; see the [pre-launch guard](nested-isolation-guard.md). The [Japanese historical source](workspace-isolation_jp.md) is unchanged.

## Scope and trust boundary

Isolated mode prevents Codex tasks in the same runtime from reading or changing one another's workspace, state, and logs. The local operator, runner source, allowed verification programs, installed Codex binary, OS, and kernel are trusted. This is not a VM boundary against a malicious operator controlling the same host UID, kernel vulnerabilities, or resource exhaustion. Legacy shared-workspace mode remains serial/read-only; use `--isolated` for the stronger workspace boundary.

## Ownership and layout

```text
<runtime>/
  tasks.sqlite                 # coordinator-owned database
  runner.lock / leases/        # execution leases
  fixtures/<name>/             # dedicated operator-provided input
  workspaces/<workspace_id>/
    workspace/                 # task artifacts and verification targets
    codex-home/                # Codex state for this workspace only
    tmp/jail/                  # empty mountpoints; actual jail is namespace tmpfs
  snapshots/<snapshot_id>/     # independent copy after termination
  logs/ / db-backups/
```

The workspace table records ID, initial owner task, access, state, source snapshot, initial fingerprint, and creation/release time. Tasks and executions also record workspace/role/access/snapshot. New tasks receive new workspaces; only explicit resume inherits the source workspace after checking owner ancestry and access. A lease rejects simultaneous use.

Fixture inputs are single names under `runtime/fixtures`, not arbitrary absolute paths or product checkouts. Reject ancestor/internal symlinks, hardlinks, non-regular files, traversal, nested mounts detected through `/proc/self/mountinfo`, and project `.codex` overrides. Limits are 1,000 regular files, 16 MiB per file, about 64 MiB total, and 1,000 directories. Copy into new files, never shared hardlinks; preserve empty directories and owner execute bits. Compare regular-file manifests before/after the source copy and at the destination.

## Linux boundary

1. Create a dedicated child's user/mount/PID/IPC namespaces, parent-death guard, kill-child behavior, and private proc.
2. Make mount propagation private and construct a new tmpfs root.
3. Bind `/usr`, `/etc`, standard libraries, and required Codex installation directories read-only.
4. Bind only the owned session root. Keep its root read-only, its Codex home writable to Codex, and its workspace mounted according to task access.
5. Bind existing `auth.json` read-only without opening/copying its contents in Python. Do not mount the original HOME.
6. Create minimal devices, private proc/tmp/var-tmp, and required resolver files.
7. `pivot_root`, detach the old root, and leave no host cwd/source FD behind.
8. Drop capabilities including SYS_ADMIN/SYS_CHROOT, retaining only SETUID/SETGID/SETFCAP in the bounding set for required nested-user-namespace behavior.
9. Run Codex's own read-only/workspace-write sandbox inside that outer boundary.

Other workspaces, coordinator DB, original HOME, and daemon sockets are absent from the new root. Historical OS tests checked that `..`, absolute paths, symlinks, and `/proc/1/root` could not recover the old root. Separate mount namespaces leave the original host mount tree unchanged.

See [pivot_root(2)](https://man7.org/linux/man-pages/man2/pivot_root.2.html) and [unshare(2)](https://man7.org/linux/man-pages/man2/unshare.2.html). The initial chroot approach failed because of nested-user-namespace restrictions; the phase used pivot_root with measured nested-sandbox behavior, not weaker permissions as a fallback.

## Read-only and workspace-write

Default access is read-only, enforced by mounts rather than model instructions. Workspace-write requires an explicit task, dedicated fixture, artifact content SHA-256, and allowed tests. Check effective thread sandbox/approval, and explicitly send:

```json
{"type":"workspaceWrite","writableRoots":["<own-session>/workspace"],
 "networkAccess":false,"excludeSlashTmp":true,"excludeTmpdirEnvVar":true}
```

The outer private tmpfs serves infrastructure, not an additional model-writable area. Authentication mounts remain read-only; refresh requiring writes fails. Isolated executables must be system programs under `/usr` or the installed Codex binary. Do not mount another workspace as an execution resource.

The trusted App Server uses authentication; independent verifiers instead hide all of `CODEX_HOME` with an empty mount and use a separate network namespace. Failed namespace/nested-sandbox preflight blocks dispatch. There is no ordinary-launch or danger-full-access fallback.

The phase compared [App Server documentation](https://learn.chatgpt.com/docs/app-server) with the installed binary's generated schema. That binary supported the five fields above but lacked newer `readOnlyAccess` restrictions. Unsupported fields were not sent; outer mounts enforced reads. `experimentalApi=false` remained in use. Recheck schema for another binary.

## Review, snapshots, and release

Create a snapshot only after a known terminal implementer state, confirmed cleanup, and acquisition of the workspace lease. Record source task/execution/workspace and fingerprint; make snapshot files read-only. A reviewer gets an independent workspace copy, never the source's in-progress mount. `role=reviewer` requires a snapshot and read-only access, rejects fixture/write inputs, and checks tampering at submission/start.

The reviewer has independent thread/turn/execution, acceptance, result, and logs. It does not approve or alter the implementer's state. A role is task metadata, not a fixed model/personality.

Execution leases and mounts release on termination, cancellation, or parent death. `release` marks a terminated workspace `RELEASED` and rejects reuse; it refuses while associated `QUEUED`/`RUNNING`/`VERIFYING`/`UNKNOWN` tasks remain. Artifacts, conversations, and logs are not deleted. Unreferenced directories left before DB commit are preserved for investigation, not automatically executed or removed.

## Limits and later resource work

Fixtures cover small regular files, not reproduction of Git metadata, special files, ACLs, or xattrs. Fingerprints include regular-file names, sizes, and hashes, not execute bits, empty directories, or transient effects. Those copied properties need explicit tests when part of acceptance. Concurrent malicious same-UID modification is outside this threat model.

Large/adversarial inputs, other kernels/OSes, and OS restart were not verified by C. Git worktrees were only a future input-method candidate, not a replacement for mounts or leases. D1 added private outer cgroup scopes without weakening namespace/capability restrictions; see [resource limits](resource-limits.md), including the lack of a hard total-disk quota.
