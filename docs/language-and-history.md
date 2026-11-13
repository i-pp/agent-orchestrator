# English Documentation and Historical Sources

English is the primary language of the runtime's product, setup, usage, and architecture documentation. Canonical filenames have no language suffix. Follow the shared [technical documentation policy](https://github.com/i-pp/ai-orchestration/blob/main/docs/ai/documentation-policy.md).

## Historical Japanese sources

The 21 Japanese historical sources added with the non-native English guide conversion preserve their original bytes. The four `native-*_jp.md` snapshots are a separate case: they preserve historical text and technical values, but add a status notice and normalize table whitespace/H1 placement for lint. Their exact original bytes remain in the integration baseline Git history. They are historical originals, **not maintained translations of the new English text**. They can contain superseded policy references, old environment-specific status, and earlier feature limits. Use the English primary document and current implementation for present guidance; use the originals to trace what a development phase recorded.

The English conversion preserves the original results and limitations without presenting them as new validation. It also corrects outdated present-tense policy-location and Git-index guidance in the English guides. No live configuration, credentials, logs, databases, service state, or private conversation history was imported.

The original Japanese [phase reports](phase-reports.md) keep their unsuffixed filenames because they are historical evidence, not current product documentation. The hash-pinned legacy [`policies.md`](../policies.md) and its manifest remain unchanged. Follow [AGENTS](../AGENTS.md) for current verified policy loading; the legacy policy is not a second active source.

## Formatting exceptions without rewriting evidence

`.markdownlint-cli2.jsonc` keeps narrowly scoped exceptions for existing formatting in nine byte-preserved Japanese sources:

| Source | Existing formatting exception |
| --- | --- |
| `git-tracking_jp.md`, `login-autorecovery_jp.md`, `mailbox-grants_jp.md`, `mailbox-operations_jp.md`, `path-conventions_jp.md` | MD060, compact table-divider spacing |
| `poc-services_jp.md` | MD012, an extra blank line |
| `resource-limits_jp.md` | MD034, a systemd user-service identifier mistaken for a bare URL |
| `retention-policy_jp.md` | MD033, a boot-ID placeholder mistaken for HTML |
| `systemd-deployment_jp.md` | MD029, numbering around historical code blocks |

These are exact file/rule exceptions, not a general exemption for Japanese or historical documents. All other rules and repository-local link checks still apply. English primary documents have no such exceptions.
