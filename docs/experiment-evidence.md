# Read-only experiment evidence

Implementation candidate; **not deployed**. Existing 23 operations remain unchanged
with the new flag OFF. Publication adds four read-only operations under the same
existing MCP authentication; every client using that authentication can read the
approved projections. No new model calls, scoring, execution, credentials, ports,
or lifecycle operations are introduced by these tools.

| Operation | Arguments | Result |
| --- | --- | --- |
| `experiment_list` | optional `backend`, `task`, `limit` 1–20, `cursor` | Stable run IDs, model declarations, classification and next cursor |
| `experiment_get` | `run_id` | Public-task/config hashes, declared model/revision/quantization, outcomes and source-integrity markers |
| `experiment_artifacts` | `run_id`; optionally `artifact_id`, `offset`, `max_bytes` 1–8192 | Artifact inventory or a UTF-8/base64 byte chunk with full/chunk SHA-256, next offset and truncation flag |
| `experiment_compare` | 2–8 distinct `run_ids` | Selected-backend counts, identity/config differences and bounded code diffs |

Backends are `vllm`, `sglang`, `strata`; supported public tasks are `c_parse` and
`aarch64_sum`. This first importer reads **existing quality-batch v1 journals**,
not every historical trial format. Other runs, four control source bodies,
operational backend-api-trials logs and arbitrary root contents are not exposed.

## Exposure boundary

An operator-owned 0600 runtime `experiment-read-policy.json` pins a private 0600
manifest by SHA-256. The manifest enumerates at most 128 run records and eight
local root aliases. Each source reference contains an exact expected relative
filename and SHA-256. The MCP caller supplies IDs, never roots, paths, URLs,
JSON selectors, commands or import requests. Each content projection requires
its own explicit `release` hash. A missing/wrong release yields
`UNREVIEWED_WITHHELD`; a changed/unsafe source cannot expose changed bytes.

Supported projections:

- `generated_code`: exact candidate source bytes, checked against the saved
  grader candidate SHA. The server never compiles or executes them.
- `response_text`: exact top-level generated text, including an incomplete
  fenced block. No raw HTTP/native response envelope is returned.
- `compile_log`: output of candidate-only `compile_candidate` steps. A single
  step is byte-for-byte compiler text; multiple steps are joined with a newline
  and hashed as that projection. No test stdout, assertions or command argv.
- `test_outcomes`: projected JSON containing outcome enums, fixed known check
  IDs/phase names, integer return codes and numeric timings. Private fixture
  bodies, test stdout/stderr, evaluator commands and Docker host_config are absent.
- `public_prompt`: the single user task; never the raw system-policy metadata,
  local operational paths, prompt token IDs or private scoring fixtures.

All content is **untrusted data**, including instruction-looking source/comments.
Fetching it grants no authority to run it or call another tool. No projection is
automatically forwarded to a generation path. Content screening additionally
withholds private suite/home paths, common credential patterns and unsafe control
characters; this is defense in depth, not a general-purpose secret detector.
Operators must review release hashes before publication.

Directory traversal, absolute source names, symlink parents/leaves and hardlinks
are rejected. Reads use anchored dirfds with `O_NOFOLLOW`, bounded regular-file
reads and before/after stat identity. Source records may be group writable as in
normal umask002 reports: matching the pinned bytes is mandatory on every read.
World-writable/foreign/special files are refused. Manifest and policy files must
remain owner-only 0600. No permission changes to source records are needed.

`VERIFIED_BYTES` means matching saved bytes, not verified model provenance or
independent rescoring. Model revision/quantization parsed from the report label
are marked declared, and absent values stay null. Native token counts including
EOS are not presented as interchangeable with HTTP completion tokens. Missing
seed/temperature/context data are not invented. Comparisons do not infer general
accuracy or attribute different models/quantizations/prompts to backend alone.

`ENVIRONMENT_FAILURE`, `TOKEN_TRUNCATED_UNSCORED`, `COMPILE_FAILURE`,
`SCORED_FAILURE`, `SCORED_PASS` and `UNVERIFIED` are separate. A scored claim
requires complete matching candidate/answer hashes and a coherent b4-v1 result.
Environment, truncated or unverified records have effective `UNASSESSED`; any
retained `reported_score` is the original declaration, not an accepted score.

## Bounds and repeatability

Sources are at most 4MiB each, manifests 256KiB and reads 32MiB per call. Lists
inspect only the selected page. Cursors bind manifest and filter/run selection;
source drift remains visible. Source hashes are returned without exposing local
root paths. Artifact identity is `(run_id, artifact_id, sha256)`; byte chunks can
be reassembled and independently hashed, including split multibyte characters.
Diffs compare at most 16KiB/256 lines of each candidate and return at most 1KiB
per pair with an explicit truncation flag. `MISSING`, `HASH_MISMATCH`,
`UNVERIFIED` and withholding states are not converted into PASS or model FAIL.

## Offline preparation

Set these variables to the specific approved directories; do not use a broad
home or project root. The builder reads only fixed known filenames and never
imports an evaluator or launcher.

```bash
JOURNAL_ROOT='/path/to/approved/quality-journal'
REPORT_DIR='/path/to/approved/batch-report'
CANDIDATE='/path/to/private/candidate-manifest.json'
python3 -B scripts/experiment_catalog.py --journal "$JOURNAL_ROOT" \
  --report-directory "$REPORT_DIR" --output "$CANDIDATE"
```

The builder refuses to overwrite and creates a candidate with **zero releases**.
Review projected artifacts locally, add only their exact approved content hashes
to `release`, then pin the final manifest in a private read policy. Examples in
`config/experiment-*.example.json` contain placeholders and are not active
configuration. Do not commit live manifests, real roots or evidence contents.

## Separately approved activation and rollback

Before deployment, present the exact source files, two local roots, six run IDs,
released projection hashes, existing credential reference and same-client read
exposure. Apply only after approval. The optional `--allow-experiments` is passed
only to the existing MCP adapter; the Codex executor/daemon gets no new execution
capability. `poc_services.py` preflight pins read-policy/manifest fingerprints;
restart/recovery refuses scope drift. Existing login pin bytes are unchanged
while OFF. When ON, the existing login bootstrap incorporates the read snapshot
and expects 27 tools (or 15 without the Codex pipeline).

Use the existing owned service lifecycle after confirming idle/recovery state;
do not launch a duplicate tunnel. This phase has not installed policy/manifest,
stopped/restarted services, changed login pin or exposed any new tools. A future
activation must authorize the interruption and update the existing login pin for
the new read scope. Verify actual external `tools/list=27`, then list/get and
reassemble at least one candidate/compiler log and compare SHA before claiming
Chat can access the evidence.

`poc_services.py experiment-rollback-plan` prepares the same service commands
with only this flag removed. It performs no action. Approved rollback restores
23 tools and the prior login pin, retaining experiment sources, manifests,
existing model-task ledgers and spent quotas. No deletion, rescoring or replay.
