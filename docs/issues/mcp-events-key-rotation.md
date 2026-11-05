# EVT-001 — Versioned master-key rotation

- **GitHub:** [i-pp/agent-orchestrator#5](https://github.com/i-pp/agent-orchestrator/issues/5)
- **Severity:** S2 Medium
- **State:** Deferred; production credentials have not been provisioned.
- **Problem:** The AES-GCM envelope and owner-HMAC store use one current key each. Replacing either key does not migrate existing callback secrets, owner bindings, or subscription lookup hashes.
- **Impact:** A key mismatch prevents existing subscriptions from being read or delivered. Codex task records remain intact, but callback delivery can stop until the original key is restored or subscriptions are migrated.
- **Workaround:** Keep each credential unchanged for the lifetime of the outbox, preserve a protected recovery copy, and keep the outbox database. Do not rotate keys in place. Signing-secret refresh is separate and already supports a short overlap.
- **Revisit when:** Before enabling routine key rotation, add versioned key identifiers, multi-key decryption, a transactional re-encryption migration, and restart/recovery tests.
