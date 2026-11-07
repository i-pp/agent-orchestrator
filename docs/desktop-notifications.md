# Local desktop notification fallback

This opt-in worker delivers sanitized Codex task states to an existing Linux
freedesktop notification service. It does not execute or resubmit tasks, create
MCP Events subscriptions, send network callbacks, or resume ChatGPT conversations.
It is for one local user, not a multi-user notification ACL.

## Behavior

The worker reads the existing pipeline ledger in SQLite read-only mode once per
second. Notifications contain only task UUID, fixed state and UTC timestamp for
COMPLETED, FAILED, CANCELLED, WAITING_APPROVAL and actionable BLOCKED events.
Prompts, answers, logs, paths and credentials are excluded.

An independent private SQLite queue preserves the event cursor and delivery
receipts. Initial startup establishes a baseline at the current ledger head.
Later restarts recover events recorded after that baseline, including downtime.
Event-ID deduplication and an exclusive delivery lock prevent repeated polling
or concurrent workers from producing duplicate sends. Do not reset the cursor
or rerun a task to retry its notification.

The sender checks desktop-service availability before calling Notify. A known
pre-send failure is retried with 1, 2, 4, 8, 16, 32 and 60 second backoff, up to
eight attempts. A failure after Notify may have been accepted, so it becomes
UNKNOWN and is not automatically resent. Interrupted sends also become UNKNOWN.
A DELIVERED receipt proves service acceptance, not visible-banner display or
user attention. Do Not Disturb and desktop settings still apply.

## Operator setup

Prerequisites are an existing operator-owned project with a private `.runtime`
anchor, selected runtime and logs directory (all mode 0700), a private existing
`codex-pipeline.sqlite3`, Python 3, `/usr/bin/gdbus`, and a running graphical user
session. No packages, credentials, service settings or permissions are changed
by this branch. Validate prerequisites before installing anything.

The standalone worker deliberately avoids changes to the existing HTTP/Events
worker or service. Set absolute paths for your existing runtime:

```sh
python3 -B scripts/desktop_notifications_worker.py \
  --runtime /absolute/path/to/runtime-project/.runtime/runtime-name \
  --runtime-project-root /absolute/path/to/runtime-project
```

This starts local notification delivery. For a user service, adapt every path
in `systemd/aceis-codex-desktop-notifications.service.example`, confirm that the
graphical session supplies its session bus, and review the resulting unit before
installation. The example is not installed automatically. Never start a second
monitor against a runtime already monitored by an existing service.

Read an existing queue without creating files, updating its cursor or sending:

```sh
python3 -B scripts/desktop_notifications_worker.py \
  --runtime /absolute/path/to/runtime-project/.runtime/runtime-name \
  --runtime-project-root /absolute/path/to/runtime-project --status
```

A missing queue refuses status rather than creating it. Symlinked paths,
non-private runtime anchors, and a runtime outside the explicitly selected
project are rejected. UNKNOWN or FAILED receipts require operator review.

Stopping an installed fallback does not affect Codex, HTTP or webhook delivery:

```sh
systemctl --user disable --now aceis-codex-desktop-notifications.service
```

Keep its queue and receipts for reconciliation. Replacing the source ledger or
regressing its sequence is refused until reconciled; do not delete the queue.

## Verification and deployment boundary

Run the dedicated fixture tests and the existing notification regression suites:

```sh
PYTHONPATH=tests python3 -B -m unittest \
  test_desktop_notifications test_desktop_notifications_worker \
  test_notification_outbox test_notification_runtime_composition \
  test_mcp_events_lifecycle test_notification_host_bridge -v
```

The existing host candidate previously demonstrated synthetic local service
acceptance in 36.194 ms. Its durable queue later recorded one real terminal
event accepted in about 760 ms. These are historical observations of the host
candidate, not live validation of this extracted branch, visible-banner proof,
or ChatGPT recovery. No services are changed or live notifications sent while
extracting this branch. Reboot/login restoration and end-to-end ChatGPT
delivery remain unverified. HTTP-only lifecycle restoration is tracked in
[issue #10](https://github.com/i-pp/agent-orchestrator/issues/10).

The client now exposes an MCP Events subscription tool, but subscription
acceptance, callback verification, callback delivery, conversation resumption,
and latency remain separate checks. Completed historical tasks are not
resubscribed or re-executed for testing.
