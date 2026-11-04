# npm Codex package mounts in isolation

**Status: npm package and runtime staging are implemented; host namespace validation is pending.**

`src/isolated_entry.py` now resolves the npm `codex` bin link to its canonical
`@openai/codex/bin/codex.js` target, validates the package relationship and
platform dependency, and passes that same canonical path to `exec`. It supports
the npm Linux x64 and arm64 package layouts. A system executable under `/usr`
continues to use the existing `/usr` read-only bind.

For npm installs, the canonical Codex package root is bound read-only. The
package's `node_modules` is then covered by a small `nosuid,nodev,noexec` tmpfs,
and only the declared platform package (`@openai/codex-linux-x64` or
`@openai/codex-linux-arm64`) is bind-mounted read-only at its expected location.
The masking tmpfs is remounted read-only before pivoting into the jail. This
keeps the wrapper's `require.resolve()` lookup working without exposing the
global npm parent, sibling packages, or arbitrary directories. Any unsupported,
incomplete, mismatched, symlinked, or unmountable package fails closed before
Codex executes.

The existing authentication mount, no-auth network namespace, workspace access
mode, minimal root, and nested Codex sandbox behavior are unchanged. The
read-only mount protects files from writes made inside the isolated process; it
does not freeze the host-side npm installation against concurrent changes, and
the package manifests are consistency checks rather than a cryptographic
authenticity proof. The installed Codex package and selected Node executable
remain trusted host inputs.

The npm `codex.js` wrapper has a `/usr/bin/env node` shebang. The launcher uses a
deliberately restricted `PATH`, so `isolated_launch()` resolves the Node
executable selected by the host, validates it as an executable file, and passes
that one path to the private entry process. The entry mounts only that file
read-only when it is outside `/usr`, then invokes Node directly on the canonical
`codex.js` path. It does not add the Node parent directory to `PATH` or expose
its sibling files. A missing or invalid Node runtime fails closed. Node is
removed from the environment before Codex starts.

## Verification

Offline fixture tests cover the canonical npm bin symlink, package metadata and
dependency matching, the one-dependency mount plan, Node executable validation
and exact-file staging, missing or malicious layouts, and mount failure
propagation. The host harness reports only an allowlisted failure class, exit
status, and last inner-canary stage; it never prints raw stdout/stderr or
filesystem paths. `test_version_isolation.py` also contains host-only checks
for isolated `codex --version`, synthetic read-only authentication, a synthetic
host secret, a synthetic Unix daemon socket, and existing no-auth/network
behavior. These tests skip when the repository's isolation guard detects a
sandboxed or unknown context; they do not bypass it.

On the target host, the user's reported temporary
`kernel.apparmor_restrict_unprivileged_userns=0` setting is only observed; this
code and the validation commands do not change or persist it or edit AppArmor
profiles. From the independent checkout, run:

```bash
python3 -B -m unittest discover -s tests -p 'test_isolated_entry.py' -v
python3 -B -m unittest discover -s tests -p 'test_host_validation_diagnostics.py' -v
python3 -B -m unittest discover -s tests -p 'test_version_isolation.py' -v
python3 -B -m unittest discover -s tests -p 'test_isolation_guard.py' -v
python3 -B scripts/validate_npm_codex_isolation_host.py
```

The layout, diagnostic, and guard suites are offline. The version-isolation suite uses only
synthetic auth canaries, never the real auth file; its actual Codex `--version`
path is no-auth and network-isolated. The host harness checks the runner's
preflight path and intercepts `login status` before execution, so it does not
read real auth or submit a model turn.

Expected host-only results include `CODEX_NODE_RUNTIME=PASS`, a passing isolated `codex --version`,
`RUNNER_CHECK_PREFLIGHT=PASS`, `RUNNER_CHECK_AUTH_STAGE=REACHED_BUT_NOT_RUN`,
passing synthetic auth/read-only and host-file/socket canaries, and no namespace
guard block. If the environment reports `BLOCKED` or `UNKNOWN`, retain that
result; do not retry through nested isolation or use an unisolated fallback.

## Single-command host integration run

Run this once from the independent checkout's normal host terminal:

```bash
python3 -B scripts/validate_npm_codex_isolation_host.py
```

It first checks the repository isolation guard and reads AppArmor enabled state
and the current sysctl. It reads both again after cleanup. A `SANDBOXED` or
`UNKNOWN` context exits before creating fixtures or namespaces.
In `HOST_CONTEXT`, it creates only a disposable runtime, synthetic HOME/auth
file, fake secret file, and ephemeral pathname/abstract/loopback socket
listeners. It runs the real npm Codex `--version` through the no-auth,
network-isolated launch. It follows the `runner.py check` identity path through
the actual sandbox preflight, then intercepts the `login status` subprocess
before execution; this records whether the auth gate was reached without
reading real credentials. It also checks outer and Codex-provided read-only
mounts, synthetic auth visibility, host canaries, and runs the five regression
suites listed in its output. The Codex sandbox invocation runs only a local
Python canary; no model request is made.

The harness passes a sanitized environment to subprocesses, never prints raw
stderr or paths, does not contact the real daemon, does not change host
configuration or services, and removes its temporary runtime and socket files.
Share the script's key-value output lines only. `RUNNER_CHECK_AUTH_STAGE` is
deliberately reported as `REACHED_BUT_NOT_RUN`; a full `runner.py check` with
real account authentication is outside this no-real-auth validation.

## Current execution evidence (2026-11-04)

- The installed npm package metadata and binary layout validated locally as
  Codex CLI `0.165.0`, with `@openai/codex-linux-x64` selected. This is a
  layout-validation result, not an isolated version run.
- `test_isolated_entry.py`: 10 passed. `test_host_validation_diagnostics.py`:
  5 passed. `test_version_isolation.py`: 3 passed, 4 isolation-required cases
  skipped as `BLOCKED`. `test_isolation_guard.py`: 12 passed.
- `test_runner.py`: 22 tests ran; 9 passed, while 12 failed and 1 errored
  because the runner's required verification isolation was blocked and the
  tests expected fake tasks to complete. These are `WAITING_FOR_HUMAN` guard
  outcomes, not model-quality results; the host-only runner regression remains
  unverified in this environment.
- `runner.py check` was invoked with a temporary runtime and synthetic `HOME`.
  It returned `BLOCKED`, `NESTED_ISOLATION_FORBIDDEN`, `executed=false` before
  opening auth or starting a subprocess. The host-only `codex --version`, real
  outer mount/read-only checks, synthetic auth/secret/socket canaries, and
  cleanup checks remain unverified until run from an authorized normal host
  context.
- The current execution environment's AppArmor restriction sysctl was read as
  `0`; no sysctl or AppArmor setting was changed. That observation does not
  override the nested-isolation guard.
- The single-command host harness was invoked here and reported
  `HOST_CONTEXT=SANDBOXED`, sysctl `0`, then exited `BLOCKED` before creating
  temporary files or namespaces. Its actual host integration stages remain
  pending.
- The user's latest target-host run reported package detection, runner
  preflight, outer auth/secret/socket/read-only canaries, cleanup, and the
  isolation-guard and runner suites as passing. It reported
  `ISOLATED_CODEX_VERSION=FAIL (CalledProcessError)`, the inner Codex canary as
  `FAIL (RuntimeError)`, and the version-isolation suite as `FAIL (exit 1)`;
  later inner checks were therefore not established. No raw child output was
  included, so the original failure stage is unknown.
- The newest reported target-host run is still `OVERALL=FAIL`: package
  detection (`0.165.0`), runner preflight, outer read-only/secret/socket
  canaries, cleanup, isolation-guard regression, and runner regression pass;
  isolated `codex --version` exits 1, the Codex inner package/read-only/auth/
  secret/socket canary fails, and version-isolation regression fails. The
  synthetic auth gate was reached but not executed; no real authentication or
  model request ran. This candidate remains fail-closed and is not usable for
  Codex execution until those required checks pass.
- Read-only inspection in this executor found the host-selected Node path under
  the user's NVM tree, while `/usr/bin/node` also exists and reports the same
  Node version. This makes a simple missing-Node explanation unlikely; it
  does not establish the cause of the earlier failure. The updated launcher
  aligns the npm wrapper with the host-selected Node executable and adds
  secret-free stage diagnostics for one follow-up host run.
- The target host has not rerun the updated harness yet. Its prior run observed
  `kernel.apparmor_restrict_unprivileged_userns=0`; the implementation and
  harness do not change or persist that value or modify AppArmor profiles. The
  candidate remains unselected until the isolated version, inner canary, and
  host-protection checks pass on the target host.
