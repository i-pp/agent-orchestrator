# Loading the Pinned Common Policy

The source of truth is [i-pp/ai-orchestration](https://github.com/i-pp/ai-orchestration). This repository owns runtime execution, monitoring, task management, and the verified loader. It does not maintain a second copy of the current canonical policy bodies or automatically edit, pull, reset, or synchronize their checkout.

## Adopted source and hashes

`config/upstream-policy.json` records the adopted canonical commit, `git:<commit>` bundle version, aggregate SHA-256, and SHA-256/Git blob SHA for each of five documents:

- `docs/ai/orchestration.md`
- `docs/ai/policies.md`
- `agents/README.md`
- `docs/ai/editorial-policy.md`
- `policies/editorial.md`

`config/policy.json` requires this upstream pin. Editorial Policy's document version is 2.0; it is not the version of the entire Git-pinned bundle. Inspect the actual configuration and verified metadata rather than assuming the latest branch head is accepted.

The loader reads complete immutable local Git objects, checks every document hash and the assembled bundle hash, and does not fetch, clone, copy, or follow a remote branch automatically. To change adoption, first review and identify the accepted canonical commit, deliberately update all pin metadata, and run regressions. Never rehash arbitrary content just to silence an error or fall back to an older policy when the selected source is unavailable.

## Existing checkout selection

The portable default `../ai-orchestration` is relative to the **runtime project root**. `~` expands to the executing user's home; environment variables are not implicitly expanded. An operator-local selector can use an absolute path. The loader checks existence and rejects symlinks and checkout subdirectories.

Normally use an existing checkout with canonical origin and the pinned commit object. Verify its remote, HEAD, and dirty status; a path example is not proof that a checkout exists. Do not overwrite uncommitted work with pull/reset or create another clone automatically.

An originless checkout or personal fork can be a content carrier only when the operator explicitly supplies its local commit and all five document byte sequences match the canonical pin. It is not treated as origin-verified. Canonical commit, actual loaded local commit, HEAD, dirty state, and origin proof are recorded separately. This does not permit trusting another repository's text or changing the canonical hashes to match it.

The optional `config/policy-source.local.json` is Git-ignored and contains no credentials. Use the [example](../examples/policy-source-checkout.example.json) with an existing path and full 40-hex commit:

```json
{
  "schema_version": 1,
  "checkout": "../ai-orchestration",
  "checkout_commit": "<40-hex-local-commit>"
}
```

The selector never disables hash validation. Metadata includes `checkout`, `loaded_checkout_commit`, `checkout_head_before/after`, `checkout_dirty`, `checkout_origin_verified`, and `local_commit_override`. The body always comes from the selected commit's Git objects, not dirty working-tree text. Advancing HEAD does not adopt a new policy; losing the required object causes a load failure.

## Inspection, delivery, and precedence

```bash
python3 -B scripts/policy_context.py --format inspect
python3 -B scripts/policy_context.py --format packet --target dot \
  --task 'Inspect the assigned outcome and acceptance criteria.'
```

The path is `common_policy.load_project_policy` -> fixed source configuration -> `upstream_policy` -> existing local Git objects -> hash validation -> immutable bundle. Each document appears once in developer instructions, with metadata saved to the execution owner. Runner fresh/resumed threads, repository instructions, native profiles, and the common-policy skill use this same loader. A recorded version/hash mismatch blocks resume before starting a model.

Higher-priority execution rules, permissions, explicit user constraints, and existing safety guards take precedence. Within those boundaries, apply the pinned canonical policy, project/role details, and task scope. The policy does not grant approval, override no-nested-isolation requirements, widen publication/authentication/cost scope, or weaken schema/sandbox checks. Body delivery is not proof that a model followed it.

Unavailable sources, bad configuration, commit/version/hash mismatch, or rejected paths raise `PolicyError` and block affected model dispatch. Diagnostic reads and independent safe work can continue. Constructing a packet is not automatic delivery or evidence of a successful direct parent-Dot invocation.

## Editorial v2 and intentional transitions

The three governance documents are combined with the authoritative Editorial Policy v2 and its supplement at one accepted commit. All five documents and their aggregate are verified. The legacy root `policies.md` v1.0 remains a separate immutable historical record, not a second current injected body.

Apply reader value, evidence integrity, independent technical/editorial review, and accepted-B freeze before creating A when technical articles are in scope. Do not force article structure onto ordinary implementation results. Face C remains independent: do not send it shared policy/skills, manufacture its persona, or infer contact/publication/merge authority.

Only the pinned five bodies are injected. Referenced writing skills, examples, and `load_policy.py --show` live in the canonical checkout; injection does not install them into a runner workspace. Read any needed references separately at a permitted fixed revision and record their identity. Packet/fixture success does not establish article quality, model compliance, acceptance, or publication readiness.

When adopting a new pin, an existing explicit local selector must point at a commit whose five complete documents match it. An old three-document selector, missing file, or hash mismatch fails closed. A resumed execution with different recorded policy identity also fails before a new model call. Preserve old owner metadata and frozen manuscript bindings; use a deliberately reviewed new task rather than rebinding history. A resume without older policy metadata retains the existing `legacy-unpinned` classification.

A source merge and a rollout to an execution environment are different steps. Do not switch an existing daemon, notification worktree, task database, authentication, or global configuration merely because this document or the source pin changed.

## Legacy record

The user-authored `policies.md` v1.0 retains SHA-256 `8fede5f946fd944485e7095ea27cb4923fb97ebf466d1e4b355ad21a50a00286` and its manifest unchanged. Adoption validates that historical reference but does not inject it alongside the current upstream body. Do not translate those pinned bytes in place or maintain two evolving policy sources.

The canonical source separates governance from runtime, uses Routine/Controlled/High impact risk classes rather than the old S0-S3 scheme, and states that role/delegation never grants authority. Detailed numeric DAG/concurrency/retry limits live in current project configuration; historical values are not silently removed or treated as new completed enforcement. Current native limits are documented in the [native team guide](native-agent-team.md). Actual authorization already granted is not requested again unless scope or risk changes.

## Verification boundary

Inspect the actual pinned five documents and metadata in an existing checkout. Offline Git fixtures cover unavailable objects, tampering, dirty-state preservation, different HEAD, explicitly equivalent local commits, version mismatch, path resolution, section boundaries, and immutable context revalidation.

Runner/simulated App Server checks inspect delivered developer instructions, owner metadata, fail-before-dispatch behavior, and resume. They do not establish a fresh live inference, automatic compliance in every environment, or deployment. These checks do not modify the canonical checkout, create nested isolation, or change authentication.
