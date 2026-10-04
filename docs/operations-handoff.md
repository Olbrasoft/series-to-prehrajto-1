# Operations handoff

## Original-source priority; alternate searches paused (October 4, 2026)

The user requested that difficult subtitle cases be recorded for later while
available original tracks receive priority. `ALTERNATE_SUBTITLE_SEARCH_PAUSED`
is now `true`. The discovery workflow skips its job and the watchdog does not
dispatch it. Do not resume alternate searches without an operator request.
Missing original tracks still produce durable handoffs; video uploads continue.

The generated `CZ Titulky` name and `source_lang_class=CZ_SUB` do not prove that
a source has a track: foreign audio is also assigned this classification.
Attachment now prioritizes prepared files, positive track evidence for the exact
original source (including previous fetch failures), and then identity-bound
source title hints. Historical original-source success rates order equally
hinted series. These hints only affect order: the actual source is resolved and
its Czech track confirmed before use. Target availability cooldowns still apply.
Source evidence is shared across both accounts by exact original URL identity;
each account worker still attaches only to its own uploaded video IDs.

Original sources are checked before destination pages. A missing track causes
one source check and a handoff, with no account search or target resolve. A
positive result is reused for attachment rather than resolving the source twice.
The destination ID and absence of an existing Czech track are still verified
before POST; pending/uncertain submissions are never reposted. Fresh submissions
are verified before older processing records so positive work is promptly
confirmed. `--no-source-first` retains the old ordering for diagnostics.

Acceptance run `37201521518` selected its own episodes on both accounts and
sent eight attachments in a 5m26s account-processing window. Independent fresh
HTML and CDN downloads verified seven exact SRT matches. Episode 64729 / primary
advertised a new Czech track but its CDN file returned HTTP 404; its report was
corrected to `subtitle_processing` with the actual POST time, preserving the
verification-only invariant. Verification now requires downloadable timed cues,
so an advertised but unavailable file cannot count as a completed attachment.
The [acceptance record](subtitle-priority-acceptance-2026-10-04.json) documents the
results; 170 automated tests passed. This is an initial batch, not a sustained
daily-throughput measurement.

## Separate original and alternate subtitle queues (October 4, 2026)

Attachment now performs only original-source checks, prepared-file attachment
and verification. A conclusive missing/unusable original source is persisted
as `alternate_search_pending` with `source_lane=alternate`, source identity,
check time, target duration and a weekly original recheck deadline. These rows
are excluded before any fast-worker HTTP request. Losing the discovery artifact
does not put them back in the original queue. Transient original failures use
`source_retry_pending` with a six-hour delay; rate limiting ends the batch.
Changed source identity or title matcher reopens the original check. Submitted
or uncertain attachments remain verification-only, including targeted runs.

Existing negative checks made with the current title matcher migrate to the
alternate queue without another source request. Transport failures and older
matcher results do not migrate: older results receive one corrected check.
`src/subtitle_queue_status.py` reports fast, alternate and verification counts.
Queue-next dispatches only actionable fast/verification work, so an alternate
backlog alone cannot create a chain of empty attachment jobs. The watchdog
recovers missing workers and spaces discovery recovery starts by 15 minutes.

Discovery selects only handed-off rows. Its artifact persists query results,
candidate cursor, completed queries and checked video identities, with no signed
subtitle URLs. Interrupted searches resume at the unchecked candidate. Accounts
alternate, including across runs. Each episode gets at most a 180-second search
budget; runtime exhaustion retries after 15 minutes. Within each account, due
saved searches take priority over unseen rows so a large backlog cannot starve
resumption. While at least 100 original
checks are due, discovery gets a 120-second batch and a 60-second request gap to
favor the original sweep. This is a conservative budget, not a shared global
rate limiter. Successful backfill also triggers discovery; prepared files wake
attachment only when no attachment run is already active or queued.

October 4 acceptance passed 156 automated tests. A targeted production run
attached the original Czech track to episode 101640 / primary; fresh public
HTML and downloaded VTT confirmed all 354 cues exactly matched the prepared
SRT. A read-only live discovery test for episode 59117 exhausted a one-second
budget, restarted from its saved query/candidate cursor, and completed the
remaining queries without repeating earlier searches. No alternate track was
found for that negative case. See the [acceptance record](subtitle-acceptance-2026-10-04.json).

## Subtitle discovery and attachment (October 3, 2026)

`prepare-subtitles.yml` searches and downloads Czech subtitle files for both
accounts independently of attachment. One shared discovery worker runs every
15 minutes, uses a 30-second proxy/search interval, stops searching on transient
failures, and retains at most 100 ready SRT files. Files and discovery cooldowns
are stored in the `subtitle-source-cache` Actions artifact (one-day retention),
not Git history. Losing this cache only repeats discovery; attachment history
remains in the existing report. No duplicate video uploads are created.

`backfill-subtitles.yml` restores that cache and prioritizes prepared files.
It can still attach subtitles from the original source immediately, but slow
alternate searches now run only in discovery. Cache entries are bound to the
account, episode, current destination video ID, original source, checksum and
target duration. Submitted/uncertain POSTs remain verification-only.

Source titles recorded for the exact uploaded source can supply an original
series alias when their season and episode match. Mismatched episode numbers
are rejected. Old negative title results with a valid new alias are revisited
once; successful or deleted targets are not reopened.

Missing targets retry after 24 hours, processing videos after two hours, and
unresolved targets or deferred source discovery after six hours. Subtitle
submissions older than a day are verified every six hours. Explicit targeted
runs bypass these time gates. Discovery without a track retries after a week.

While at least 900 upload-ready episodes and 100 subtitle candidates remain,
the watchdog and both video source preparation entrypoints yield search
capacity to subtitles. Below 900 ready episodes, normal source preparation
resumes. Video uploads remain enabled on both accounts.

For a targeted acceptance test, dispatch backfill with `episode_id`, the
correct `account`, `continue_backfill=false`, and `verify_immediately=true`.
Then run `src/verify_subtitle_delivery.py URL --video-id ID` to independently
fetch fresh public player HTML, confirm the exact video ID, and download and
parse actual Czech subtitle cues. `--expected-srt` additionally compares all
delivered cues against the prepared file. The resulting JSON omits signed
CDN URLs and credentials. HTTP 200 on the attachment POST alone is insufficient.

Live acceptance on October 3 passed on both accounts: episode 87916 / primary
received 324 Czech cues from its original source; episode 101642 / serialy
received 474 cues through the discovery artifact. Both public pages had no
Czech track before the test. Fresh HTML confirmed the exact destination IDs,
and the delivered VTT normalized to the exact prepared SRT hashes. The
[acceptance record](subtitle-acceptance-2026-10-03.json) includes run links,
timestamps, cue counts and hashes; 139 automated tests passed. Regular
two-account backfill and discovery were dispatched again after acceptance.

## Video uploads resumed (October 1, 2026)

The user explicitly requested resumption on both accounts after confirming that
Prehraj.to had processed the previously uploaded videos. The September 26 pause
is over; normal continuous-upload health targets apply again.

In `Olbrasoft/series-to-prehrajto-1`, `sync.yml` is enabled and the repository
Actions variable `VIDEO_UPLOADS_PAUSED` is `false`. Run `36868127402` was
dispatched at 13:21 UTC with four shards (0/1 primary, 2/3 serialy), eight
episodes per shard, and automatic continuation enabled. Subtitle backfill
remains enabled for both accounts and continues independently.

Keep the workflow enablement and pause variable in agreement. For any future
operator-requested pause, set `VIDEO_UPLOADS_PAUSED=true`, disable `sync.yml`,
and cancel active and pending sync runs; changing the variable alone does not
interrupt running jobs. Resume only after an explicit user request. Verify
actual upload progress from fresh per-account upload timestamps, and subtitle
progress from report timestamps and playable Czech tracks.

## Whisper dependency compatibility (October 1, 2026)

Unconstrained installs selected PyAV 19, which removed the `metadata_errors`
argument still passed by faster-whisper 1.2.1. Audio decoding then raised a
`TypeError` in both language audit and Whisper review. All three workflows
that install Whisper now share `requirements-whisper.txt`, pinning the tested
faster-whisper 1.2.1 / PyAV 18.0.0 combination. Their installation steps also
decode and resample a generated WAV through the real Whisper decoder, without
downloading a model, to catch future dependency incompatibilities early.

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
- Workflow logs contain detailed resolve, download and upload diagnostics.
  Runtime `state/*.log` files are intentionally ignored and are not committed.
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
- Workflow-level concurrency group: `series-to-prehrajto-1-sync` with
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
  `series-to-prehrajto-1-prepare`.
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

## Subtitle throughput (September 2026)

### Incomplete subtitle discovery (October 2)

A search HTTP 429 was previously swallowed, so a partially failed search could
record terminal `source_track_not_found`. Source resolution outages, failed
search queries, and transient alternate-candidate resolution failures now keep
the episode as `source_search_pending` with a credential-free reason. A 429
stops further query variants immediately; the batch also defers further
alternate searches while still accepting usable original-source tracks.
Permanent missing sources and successfully completed empty searches retain
their existing handling. Episode 95701 / current primary video 29670385 was
identified for requeue from failed-search evidence in run 37009294200.

### Deleted targets and renamed videos (September 30)

`target_not_found` describes our destination video, not the source of its
subtitles. An authenticated audit of the portal's selected Deleted filter
found 1,287 `CZ Titulky` entries across both accounts. Only 863 matched the
current account/video IDs in upload state (344 primary, 519 serialy). Entries
with different IDs must not close a current replacement.

Confirmed deleted targets receive `target_deleted`, which excludes them from
normal subtitle work and remaining-work totals. The evidence must be an exact
video ID under the visibly selected Deleted filter, never an empty search,
HTTP error, or missing public stream. Account upload state is retained so the
paused uploader cannot mistakenly treat a deleted episode as never uploaded.
Cached unresolved targets and long-pending submissions also check for deletion;
the latter check at most once daily. Restored videos can be explicitly retried
with `--retry-reported`.

Profile lookup searches all folders. It tries the recorded title and then the
series/episode prefix, always requiring the immutable video ID. For example,
video 29009897 is present as `Penny Dreadful: Město andělů S01E06 - Já na bráchu
CZ Titulky`, while upload state contains `Já na bráchu...`; the previous exact
title search incorrectly reported it missing.

Source problems remain separate: `source_search_pending` means alternate Czech
subtitle discovery is deferred, and `source_track_not_found` means no suitable
track was found in the attempted sources. Neither status proves the source
video was deleted; sampled sources were playable but had no Czech track.

On September 28, 979 current videos were incorrectly excluded by terminal
subtitle results belonging to an older copy of the same episode (500 primary,
479 serialy). Subtitle history now applies only when the numeric video ID
matches the current upload, and known account identities agree. This applies
to terminal filtering, retry ordering, cached detail URLs, and submission
verification. A replacement copy is inspected independently, including checking
for existing Czech tracks before attaching anything. Pending submissions for
the same video remain verification-only, so an uncertain POST is not repeated.

`backfill-subtitles` runs one worker per account. Each worker searches the
signed-in uploaded-video listing by title and confirms the exact numeric video
ID. It streams work immediately instead of resolving an entire batch first.
The default batch allows 30 attempts and stops selecting work after ten minutes;
then the workflow merges and commits the report before queuing its successor.
Only one slow alternate-source search is allowed per batch. Other missing
sources remain `source_search_pending`, with one reserved retry slot per batch.

Subtitle submission and verification are separate. `submitted` is not success.
The next batch checks up to 100 previous submissions without polling sleeps,
with a two-minute verification budget, and records `uploaded` only after the
Czech track is present. `submission_pending`, `submission_unknown`, and
`subtitle_processing` are verification-only states, including when a retry is
requested. They must not trigger duplicate attachments or removal of existing
tracks. Long-lived processing states need investigation rather than blind
re-uploading. The `always()` report step preserves partial batches on failure.

The portal truncates long subtitle filenames, for example
`cs-1790093256-10` becomes `cs-1790093256…` in the player label. The resolver
recognizes these explicit Czech prefixes; new filenames are shorter. A
previous ten-minute verification failure on Mighty Med S01E24 was caused by
this truncation, although its Czech track was already available.

A controlled format check on 2026-09-22 used Diablero S02E03 (video 29611261):
original provider VTT submitted at 16:36 UTC remained processing after ten
minutes. Only that experimental attachment was removed and the same content
was converted to SRT. The replacement was playable by 16:47 UTC, with 461
Czech cues. S02E02 independently verified an SRT upload with 552 cues. The
28,099-byte VTT-to-SRT conversion took about 1.9 ms locally. Public source
pages exposed VTT tracks, but no original SRT download link was found. The
upload form accepts arbitrary extensions; HTTP 200 alone does not demonstrate
successful processing. Retain the tested SRT path unless a later controlled
check proves an equally reliable direct format.

## Upload starvation recovery (September 24, 2026)

A green sync can upload nothing when the prepared window is empty. During this
incident, language review repeatedly promoted already uploaded episodes, and
several overlapping discovery runs exhausted the shared search proxy's rate
limit. The review queue had over 26,000 pending sources belonging to uploaded
episodes. Review now excludes uploaded episode IDs and aliases, burned sources,
undersized sources, and already usable prepared episodes before applying its
batch limit. Multiple promotions for an episode prefer Czech audio regardless
of audit completion order.

Background discovery has one stable workflow concurrency group, with a
15-minute schedule and a ten-minute preparation budget. Foreground preparation
also stops selecting more episodes after ten minutes. Individual in-flight
requests can extend that budget. Completed episodes are checkpointed locally
every three results and on interruption, then merged and pushed by the workflow.
An exhausted HTTP 429 retry stops the batch without recording the interrupted
episode as having no usable source; completed work is preserved. Known
undersized prepared sources no longer prevent discovery from repairing them.

Whisper review also rebases unrelated upload/status commits before repeating
the expensive artifact merge. Previously, results could spend over ten minutes
in the commit step while continuous uploads kept winning the push race. An
artifact conflict still aborts that rebase and retries the existing keyed merge.

## Proxy outage recovery (September 26, 2026)

A runner repeatedly timed out connecting to the Czech proxy while all other
upload shards completed. It spent over four minutes retrying each unrelated
episode and held the workflow concurrency slot without uploading anything.
When every configured proxy fails at the transport layer throughout a resolve's
retry budget, the resolver now raises `ProxyUnavailableError`. The upload shard
exits unsuccessfully after that one retry budget, without rejecting the source
or placing it into the source cooldown. A responding proxy, direct requests,
permanent source failures, and working alternate proxies keep their existing
behavior. The final state step still preserves any earlier completed work.

`queue-next` also runs after failed upload shards, but not after cancellation,
so another runner can continue automatically. Upload HTTP diagnostics are
unbuffered to make future stalls visible in the live job log.
