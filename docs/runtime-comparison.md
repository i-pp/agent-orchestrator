# CLI versus App Server: Phase 1-A Comparison

This is an English account of the **2026-10-28 historical experiment**, not a current benchmark or installation claim. The [Japanese source](runtime-comparison_jp.md) is unchanged. Paths follow [placeholder conventions](path-conventions.md).

The host was Ubuntu 26.04.1 LTS, Python 3.14.4, and `codex-cli 0.164.0-alpha.11.1` at `<CODEX_BINARY>`. Both methods used the same read-only fixture/prompt, dedicated Codex home, and read-only existing ChatGPT authentication mount, not API keys, metered APIs, or existing daemons.

## Method and schema evidence

The first task returned fixture marker `CERULEAN-17` and nonce `ORCH-CONTINUITY-49`. Resume requested the previous nonce without restating it. CLI stdin was closed immediately because it reads extra context even with a prompt argument. Each App Server connection was a fresh stdio process; resume occurred after the prior process exited.

Timings span process launch through cleanup and are single observations, not statistical or future-speed guarantees. The phase checked [noninteractive CLI](https://developers.openai.com/codex/noninteractive), [App Server](https://developers.openai.com/codex/app-server), and [configuration](https://learn.chatgpt.com/docs/config-file/config-reference), and generated 320 schemas from the installed binary. Required/optional fields for thread/start/resume/read, turn/start/interrupt, and thread/unsubscribe were compared with implementation. Experimental flags were not used, but alpha/App Server instability remained. Schema evidence was under `.runtime/schema/` and `.runtime/evidence/schema-check.json`, not portable files included here.

## Normal and resume observations

| Measure | CLI exec | App Server stdio |
| --- | --- | --- |
| Normal task | 6.278 s | 6.741 s |
| Resume task | 3.853 s | 3.908 s |
| Output | Matching marker and nonce | Matching marker and nonce |
| Session identity | `thread.started` | `thread/start` response |
| Resume | `exec resume`, same ID/nonce | New process `thread/resume`, same ID/nonce |
| Exit codes | Both 0 | Both 0 |
| Live owned descendants after cleanup | None observed | None observed |

The experiment's private runtime retained its newly created session IDs and comparison JSON/JSONL. These were dedicated trial sessions, not existing user conversations. Timings/results alone do not establish wider reliability.

CLI exposes JSONL thread/turn/item events; callers must combine events, stderr, exit status, and cleanup. App Server adds explicit thread state, correlated RPC/errors, notification buffering, and `turn/interrupt`, at higher bidirectional-protocol complexity. Both can resume saved sessions and both still need a parent deadline. Parallel-load scalability was not verified in this comparison.

## Fault and cancellation observations

CLI tasks instructed a single `sleep 60`; stopping was checked after command execution began.

| CLI case | Recorded result | Including cleanup | Exit | Final live descendants |
| --- | --- | --- | --- | --- |
| Invalid sandbox | Rejected before generation | 0.092 s | 1 | None |
| Nine-second deadline | Timeout-equivalent stop | 10.255 s | -9 | None |
| Cancel after command start | Cancel-equivalent stop | 11.545 s | -9 | None |

`-9` means forced owned namespace/process cleanup, not task success. The selected App Server runner observed timeout at 9.095 seconds and cancellation at 10.922 seconds, including `turn/interrupt`, interrupted notification, unsubscribe, cleanup, and no live remnants. An invalid sandbox RPC returned `-32600` before generation. Historical evidence includes `cli-faults.json`, `live-checks.json`, `final-protocol-check.json`, and associated JSONL.

## Decision and failed attempts

**The runner selected dedicated App Server stdio.** Official thread inspection and turn interruption fit the requested control model without reconstructing them around CLI processes. Comparison CLI code stayed in tests; no multi-backend abstraction or SDK was added. This was a minimal local design choice, not a production-support guarantee.

The record retained failures rather than deleting assertions:

1. Unclosed CLI stdin prevented expected events until a 90-second deadline. Cleanup closed stdin and unintentionally started a turn during grace; it stopped at 92.220 seconds and counted toward usage. Immediate EOF fixed the path.
2. An initial outer bwrap setup blocked nested Codex UID-namespace creation. Conversation resume alone succeeded; the read task did not.
3. Comparison mistakenly started after failed namespace preflight and was stopped. The corrected flow refuses generation after failed preflight.
4. The historical host implementation changed outer setup to unshare/private mounts and validated a non-generating nested-sandbox probe. This is not permission to repeat nested isolation from a forbidden context today.
5. A permission-refusal turn reported completed and was initially misclassified. An outcome schema plus independent failure checks produced FAILED on retest.
6. Immediate post-kill inspection briefly mistook namespace teardown for surviving processes; bounded exit waiting confirmed cleanup.

First-attempt comparison, pre-outcome-fix, pre-cleanup-wait, and namespace-probe records were preserved. Model narration was never sufficient proof that a command ran.

## Usage and unverified scope

Logs recorded 22 turn starts, including nine stopped by cancellation/timeout and the unintended cleanup-time start. Initialization, schema generation, authentication-state checks, and direct sandbox-refusal probes did not generate models. Seventeen observed token reports were retained without naïve totals or cost conversion because unfinished CLI usage was missing and aggregation differed. The account's seven-day utilization changed from 16% to 17%, but other same-account activity prevented attribution to this experiment. No API key was created, obtained, or used.

Large output, network interruption, service limits, authentication refresh/revocation, other versions/OSes, concurrent load, and arbitrary semantic task verification were not tested by Phase 1-A.
