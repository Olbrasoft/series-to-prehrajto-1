# Operations handoff

This is the first document a new operator or Codex session should read. It
describes the production workflow as it currently runs, the state persisted in
the repository, the invariants that must not be broken, and the commands used
to verify real progress.

Last reviewed: 2026-07-01.

## Objective

Continuously upload series episodes to the configured Přehraj.to upload
accounts while preparing future episodes faster than uploads consume them. The
active upload accounts are split by global sync shard: primary shards upload to
the primary account and serialy shards upload to the serialy account.

An upload candidate is useful when it:

- belongs to an episode that has not already been uploaded,
- has a currently resolvable Prehraj.to stream,
- is at least 300 MiB,
- prefers Czech audio and otherwise enters the Whisper/subtitle workflow,
- has a stable display name containing series title and `SxxExx`,
- has a description when available; descriptions may be completed after upload.

The critical invariant is not merely that workflows are green. At all times:

1. one `sync` run should be active and its two upload shards should reach
   `Run sync batch`,
2. a successor `sync` should be pending or should be created by `queue-next`
   immediately after the current run,
3. source preparation must continue while uploads run,
4. the count from `upload_queue_status.py` should remain healthy,
5. preparation results must be committed to `plans/prepared-episodes.jsonl`.

## Data model and persistence

The repository itself is the durable operational database. GitHub Actions
commits state to `main`; concurrent writers therefore use merge/retry logic.

### Catalog and source input

- `backlog/series-episodes.jsonl.gz`: read-only export of series and episodes
  from production. It supplies episode identity, titles, ranking and any known
  metadata. Production is never modified by this project.
- `backlog/enriched-audit-queue.jsonl.gz`: source discovery/audit input.
- `backlog/language-audit-queue.jsonl.gz`: sources waiting for language audit.

Production source URLs are hints only. Preparation performs a current
Prehraj.to search for each episode and records newly discovered sources.

### Durable preparation reservoir

- `plans/prepared-episodes.jsonl`: one compacted latest row per episode. This
  is the durable reservoir of tested source selections.
- A row with `upload_ready=true` and a resolvable `selected_source` is not
  searched again during normal preparation.
- `src/merge_preparation_results.py` merges concurrent job results by
  `episode_id`; the newer `prepared_at` wins.
- Failed rows may be retried after 24 hours. A prepared row is reconsidered
  sooner when its selected source is no longer resolvable or becomes burned.
- Explicit `--refresh` is exceptional and intentionally rechecks rows.

Prepared candidates above the upload-manifest limit are not discarded. They
stay in `plans/prepared-episodes.jsonl` and become eligible when manifest slots
open. For example, finding 150 candidates while only 10 manifest slots are free
stores all 150; approximately 10 enter the immediate window and the remainder
stay in the durable reservoir.

### Active upload window

- `manifests/upload-ready.jsonl.gz`: bounded working set consumed by uploaders.
- `src/build_upload_manifest.py --limit 1000` rebuilds the window from the
  durable reservoir, excluding uploaded episodes, duplicate episode keys,
  burned/undersized/unresolvable sources and failed availability checks.
- The 1,000-row limit is not the total prepared inventory. A queue near 1,000
  means the active window is full.
- `reports/upload-manifest.json` describes the latest build. `new_count` means
  rows selected by that build, not newly discovered rows in that run.
- `src/upload_queue_status.py --no-require-description --json` is the
  authoritative count after subtracting episodes already uploaded by all
  shards.

### Upload state

- `state/uploaded-shard-0.json` through `state/uploaded-shard-3.json`:
  persistent upload/failure state. Shards 0 and 1 use the primary upload
  account; shards 2 and 3 use the serialy upload account.
- `state/sync-shard-0.log` through `state/sync-shard-3.log`: detailed resolve,
  download and upload logs.
- State is committed after each successful upload. A workflow marked
  `in_progress` is insufficient evidence; recent per-episode commits or log
  entries containing `upload done` prove progress.
- Permanently failed source IDs become burned and are excluded from future
  manifests.

### Language and subtitles

- Candidates at least 300 MiB with an explicit CZ/CS audio hint can be accepted
  as probable Czech audio.
- Ambiguous candidates go to `plans/whisper-review-queue.jsonl`.
- `audit-language.yml` and `process-whisper-review.yml` use metadata and
  Whisper language detection.
- Czech speech becomes `CZ Dabing`.
- Non-Czech speech can remain usable as `CZ Titulky`; it is also recorded in
  `plans/subtitle-followup-queue.jsonl` for later subtitle attachment.
- Audits are persisted in `audits/language-audit-latest.jsonl.gz`.

### Descriptions

- `plans/descriptions.jsonl` stores series and episode description results.
- `prepare-descriptions.yml` uses only Gemma (`gemma-4-31b-it`) with thinking
  disabled and rotates keys from `GEMINI_API_KEYS`.
- Episode source descriptions should be grounded in TMDB/available source text;
  Gemma rewrites them into short Czech descriptions rather than inventing plot.
- Upload does not block on a generated episode description. A fallback may be
  used, and `update-descriptions.yml` updates already uploaded videos later.

## GitHub Actions topology

### `sync.yml` - continuous uploads

- Scheduled every 10 minutes and dispatchable manually.
- Workflow-level concurrency group: `series-to-prehrajto-sync` with
  `cancel-in-progress: false`.
- Exactly one full sync run is active; GitHub may replace an older pending run
  with a newer pending trigger, but must not cancel the active run.
- Four matrix shards upload in parallel. The global shard modulo prevents the
  two upload accounts from selecting the same episode before either account has
  committed its completed upload.
- Checkout is shallow. Full history is several gigabytes and previously caused
  multi-minute startup stalls.
- Each shard normally uploads 20 episodes. A single download is bounded to 900
  seconds so one slow source cannot block a shard indefinitely.
- The `queue-next` job checks the remaining queue and dispatches the successor.
- `ops-watchdog.yml` and the schedule are additional recovery paths.

Healthy handoff evidence is a completed successful sync whose `queue-next` is
successful, followed within seconds by a new run with all active shards in
`Run sync batch`.

### `prepare-manifest.yml` - fast continuous preparation

- Scheduled every 10 minutes and also dispatched by the watchdog and other
  workflows.
- One active run and one replaceable pending run use concurrency group
  `series-to-prehrajto-prepare`.
- The default claim atomically reserves 60 episodes: 30 per preparation shard.
  Supporting callers may intentionally request a smaller 10-per-shard batch.
- Claims are persisted in `plans/preparation-claims.jsonl` with a TTL so two
  jobs do not search the same episode.
- Two shards share one proxy. Each waits 20 seconds between searches, producing
  aggregate traffic of roughly one request per 10 seconds and avoiding 429
  bursts.
- Each shard searches the first Prehraj.to results page, keeps candidates at
  least 300 MiB, resolves the selected stream, records language routing and
  commits partial results independently.
- `verify-growth` fails a run that completes without producing any upload-ready
  row from the claimed episodes.
- Small batches are deliberate: they checkpoint results every roughly 15-30
  minutes instead of holding hundreds of results until a very long job ends.

### `prepare-sources.yml` - deep background source discovery

- Runs longer batches in the background and persists additional prepared
  sources, Whisper candidates and subtitle follow-up rows.
- It complements, rather than replaces, the fast manifest preparation loop.
- Long runtime is normal. Confirm that the active step is
  `Prepare episode sources` and that completed runs commit
  `chore: prepare series episode sources`.

### Supporting workflows

- `refresh-backlog.yml`: refreshes the read-only production export when the
  local catalog/source queue is insufficient.
- `audit-language.yml`: runs metadata and Whisper language checks.
- `process-whisper-review.yml`: promotes audited candidates to Czech audio or
  subtitle-only preparation and rebuilds the manifest.
- `prepare-descriptions.yml`: produces Gemma descriptions and saves partial
  progress even when its time budget expires.
- `update-descriptions.yml`: applies prepared descriptions to uploaded videos.
- `verify-sources.yml`: tests source availability from GitHub runners.
- `ops-status.yml`: writes `reports/ops-status.json`.
- `ops-watchdog.yml`: runs every five minutes and after core workflow
  completions; it dispatches missing upload, preparation, language and
  description work.

## Watchdog policy

`src/ops_watchdog.py` is the central recovery controller. Current workflow
arguments request:

- minimum upload-ready target: 1,000,
- overnight/description target: 3,000 (the active manifest remains capped at
  1,000; prepared rows above that live in the reservoir),
- preparation batch: 30 episodes per shard,
- prepared source target: 10,000,
- production backlog target: up to 10,000 series / 1,000,000 episodes,
- language and Whisper work whenever their pending thresholds are exceeded.

The watchdog does not prove throughput by itself. Always compare queue counts
at two times and inspect preparation run output.

## Standard health check

Run commands serially. Do not run multiple `git fetch`/`git reset` commands in
parallel in the same checkout; they race on Git state. The repository receives
many large Action commits, so a fetch after several hours may download hundreds
of megabytes and take longer than the default command yield interval.

```bash
git fetch origin main
git reset --hard origin/main

python3 src/upload_queue_status.py --no-require-description --json

gh run list --workflow sync.yml --limit 10 \
  --json databaseId,status,conclusion,createdAt,updatedAt,event

gh run list --workflow prepare-manifest.yml --limit 10 \
  --json databaseId,status,conclusion,createdAt,updatedAt,event

gh run list --workflow prepare-sources.yml --limit 10 \
  --json databaseId,status,conclusion,createdAt,updatedAt,event
```

Inspect active jobs, not only run status:

```bash
gh run view RUN_ID --json status,conclusion,jobs \
  --jq '{status,conclusion,jobs:[.jobs[]|{name,status,conclusion,current:[.steps[]|select(.status=="in_progress")|.name]}]}'
```

Real upload evidence:

```bash
git log --oneline -30
tail -50 state/sync-shard-0.log
tail -50 state/sync-shard-1.log
```

Real preparation yield and proxy health for a completed run:

```bash
gh run view RUN_ID --log | rg \
  'claimed upload-ready episodes|Prepared [0-9]+ episodes|429 Client Error'
```

Interpretation:

- upload healthy: all active shards are in `Run sync batch` and recent episode
  commits/log entries appear,
- continuation healthy: one sync is active and another is pending, or the
  previous successful run has a successful `queue-next`,
- preparation healthy: one `prepare-manifest` is active and usually another is
  pending; completed runs report at least one claimed upload-ready episode,
- throughput healthy: the queue remains near 1,000 while uploads continue, or
  grows when below 1,000,
- source discovery healthy: `prepare-sources` is active and completed runs
  commit persisted results,
- rate limiting healthy: zero or occasional 429 responses are acceptable;
  repeated bursts indicate excessive proxy concurrency.

## Current snapshot

Snapshot taken 2026-07-01 around 12:35 Europe/Prague:

- active upload window: 990 usable rows after current uploads; latest manifest
  build contained 1,000 rows,
- durable preparation file: 24,598 episode rows, 16,883 currently marked
  `upload_ready` before applying uploaded/burned/stale exclusions,
- persisted uploaded episode count in the current status snapshot: 12,699,
- `sync`: active with all active shards in `Run sync batch`; the previous run was
  successful and `queue-next` started the current run within two seconds,
- `prepare-manifest`: active with both source-preparation shards running,
- `prepare-sources`: active in `Prepare episode sources`,
- recent preparation yields: 60, 60 and 58 upload-ready episodes, with zero 429
  errors in those sampled runs,
- one recent preparation run had successful shards but failed `verify-growth`
  because its claimed set produced no newly upload-ready row; a newer
  preparation run is active, so this is an isolated low-yield batch rather than
  a stopped pipeline,
- active manifest is full enough that preparation mainly replenishes consumed
  slots; additional valid results remain in the durable reservoir.

This snapshot will become stale. Re-run the health-check commands before making
an operational decision.

## Known failure modes and response

### Upload count does not move

1. Inspect both sync jobs and shard logs.
2. If checkout is slow, confirm `fetch-depth: 1` is still present.
3. If a shard is downloading one source for too long, confirm
   `SYNC_DOWNLOAD_TIMEOUT_SECONDS=900`.
4. Confirm a successor sync is pending or `queue-next` can dispatch one.
5. Do not restart preparation merely because upload is slow; these are separate
   pipelines.

### Preparation runs but adds nothing

1. Read `verify-growth` and the run log.
2. Check claimed count and `claimed upload-ready episodes`.
3. Confirm claims exclude uploaded, already prepared, manifest-queued and
   currently claimed episodes.
4. Check 429 frequency. Do not increase proxy concurrency blindly.
5. Inspect failed candidates; ambiguous language candidates belong in the
   Whisper queue, not in a permanent reject bucket.

### Push conflicts

Many workflows write to `main`. Never force-push. Preparation jobs preserve
their local artifacts, reset to the latest `origin/main`, merge by stable keys,
rebuild the manifest and retry. Upload state commits use pull/rebase retries.

### Queue appears capped

The active manifest is intentionally capped at 1,000. This is not data loss.
Check `plans/prepared-episodes.jsonl` for the durable reservoir. Do not infer
preparation failure merely because the active queue oscillates between roughly
980 and 1,000 while uploads run.

### GitHub cancels pending runs

GitHub concurrency retains one active and at most one pending run per group. A
new trigger may cancel/replace the older pending run. This is healthy when the
active run continues. It is unhealthy only when no active run remains and no
replacement starts.

## Safety and credentials

- Production database access is read-only. Never write to production from this
  repository.
- Credentials live in GitHub secrets and local access documents. Never commit
  or print them in logs or documentation.
- Required upload secrets: `UPLOAD_PREHRAJTO_EMAIL`,
  `UPLOAD_PREHRAJTO_PASSWORD`, `SERIALY_PREHRAJTO_EMAIL`,
  `SERIALY_PREHRAJTO_PASSWORD`, `CZ_PROXY_URL`, `CZ_PROXY_KEY`.
- Description generation uses `GEMINI_API_KEYS`, but the configured model must
  remain Gemma. Do not silently fall back to Gemini models.
- Never force-push operational state. Preserve concurrent Action commits.

## Files to read next

- `docs/series-upload-workflow.md`: detailed episode/source/language lifecycle.
- `docs/source-preparation.md`: source preparation commands and selection rules.
- `docs/language-audit.md`: metadata and Whisper audit behavior.
- `docs/ops-monitoring.md`: monitoring concepts.
- `src/ops_watchdog.py`: automated dispatch decisions.
- `src/build_upload_manifest.py`: exact active-window filters.
- `src/prepare_episode_sources.py`: claims, retries and source selection.
- `src/sync_batch.py`: resolve/download/upload behavior.
