# Source preparation

The backlog supplies episode identity and canonical/localized series titles.
Live Prehraj.to search supplies current sources; database candidates are hints.

## Discovery and selection

1. Search the localized title and `SxxExx` first. If no usable Czech-audio
   candidate is found, try alternate/original titles and `NxM` notation, up to
   four queries. A multiword subtitle after a franchise colon is also an alias.
2. Match the exact season and episode. Normalize punctuation, accents and the
   conjunctions `a`/`and`, while retaining the other words to reject spin-offs.
   For example, `Zákon pořádek - Zločinné úmysly S04E12` matches the canonical
   `Zákon a pořádek: Zločinné úmysly`, but a different Law & Order spin-off does not.
3. Keep the existing quality gate (350 MiB planning margin or a 1080p hint).
   The uploader independently requires the actual stream to reach 300 MiB.
   Exclude permanently burned source IDs from live results before ranking.
4. Prefer explicit Czech audio hints, including compact `czdabing` labels.
   Finding only subtitle/unknown-language candidates does not end discovery.
   Fetch page two only when page one has no usable Czech-audio candidate.
5. Probe up to three candidates per language category, stopping at the first
   working Czech source. One dead high-ranked source must not discard an episode.
   Explicit Czech hints do not require Whisper in the preparation step.
6. If enabled, Whisper checks ambiguous audio before subtitle fallback. A single
   audit resolves/samples a source once. Without Whisper, ambiguous sources are
   recorded in `plans/whisper-review-queue.jsonl` for later verification.
7. Subtitle fallback produces `CZ Titulky` and a record in
   `plans/subtitle-followup-queue.jsonl`. The subtitle backfill runs after the
   uploaded video is processed and records missing Czech tracks separately.
8. Save results to `plans/prepared-episodes.jsonl`. Build the upload manifest from
   usable plans, excluding uploaded episodes and burned sources.

## Bounded, diverse preparation

A batch takes two episodes per series per round, preserving episode order and
existing retry priorities within the series. A long series with no suitable
sources therefore cannot monopolize the whole batch. Failed episodes retain
24-hour retry eligibility; permanently failed source IDs are excluded.

A missing search result or non-Czech sample is not evidence that Czech dubbing
never existed for an entire series. Do not permanently suppress Czech discovery
based on such an inference. A future series-level language catalog should store
provenance, season coverage and expiry for any explicit dubbing information.
Current decisions use episode/source evidence and bounded search instead.

## Verification

Search logs expose result counts, usable candidates and Czech-audio hints.
`upload_ready` means a source was selected, not that an upload succeeded. Check
all upload shard timestamps and the logged-in account's statistics counter to
verify actual progress. Plans may still be rejected by the actual stream-size
check, and a green preparation run can produce zero usable episodes.
