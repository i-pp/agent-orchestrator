---
name: common-policy
description: Load the exact pinned common governance and Editorial Policy v2 text before policy-dependent runtime work.
---

# Common Policy

Read the project's `AGENTS.md`, then run from the repository root:

```bash
python3 -B scripts/policy_context.py --format inspect
```

Actually inspect the full verified policy body, version, aggregate and document hashes, loaded commit, origin proof, and dirty status. The loader reads immutable Git objects from an existing checkout, not its working-tree text, and does not fetch automatically. `config/upstream-policy.json` pins all five canonical documents, including Editorial Policy v2 and its workflow supplement. The legacy `policies.md` is verified historical evidence, not a second injected current policy.

A link, a heading, or a configuration update is not evidence that the policy was loaded. On a load failure, mismatch, or conflict, stop the affected work and report the cause; do not alter trust, permissions, authentication, guards, or pins to bypass it. Continue independent safe work. See [`docs/common-policy.md`](../../../docs/common-policy.md) for local checkout selection and transport boundaries.

Apply editorial rules to technical articles and their editing/handoffs, not to the output format of unrelated implementation tasks. All higher-priority rules, permissions, and explicit user constraints still apply. A policy or skill never expands authorization.
