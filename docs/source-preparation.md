# Source Preparation

Before uploading an episode, prepare its source choices:

1. Load episode metadata from backlog — series title, season, episode number.
   No DB source candidates are required; the episode title alone is enough.
2. Search Prehraj.to for the episode by title + episode code
   (e.g. `Dexter S07E04`, `Dexter 7x4`).
3. From the HTML search results, filter candidates that:
   - match the episode title and code (`SxxExx` or `x` format),
   - look Czech (title contains `CZ Dabing`, `CZ`, `český dabing`, etc.),
   - have filesize ≥ 300 MB (visible in search HTML).
4. Send browser-like headers for search requests (`User-Agent`, `Accept`,
   `Accept-Language`, `Accept-Encoding`, `Referer`, `Upgrade-Insecure-Requests`,
   and `Sec-Fetch-*`). Sparse script headers can be rate-limited even when the
   same URL works in a browser.
5. Only fetch the second search result page when the first page has no suitable
   candidate. This keeps request volume low because the first HTML response
   already contains the candidate names and file sizes needed for the first
   filter.
6. If no Czech audio source is found, allow a Czech-subtitle source as fallback.
   Mark the upload name as `CZ Titulky` and write a follow-up row to
   `plans/subtitle-followup-queue.jsonl`, because subtitles can be attached only
   after Přehraj.to finishes processing the uploaded video.
7. Audit language signals (title, metadata) and probe the stream of the first
   qualifying candidate.
8. If the stream is resolvable and Czech audio is confirmed, mark the episode
   as `upload_ready`. The first working candidate is selected — no need to probe
   all options.
9. Store both the selected source and rejected alternatives in the repository.

The source queue files (`language-audit-queue.jsonl.gz`,
`enriched-audit-queue.jsonl.gz`) are ancillary. They can provide extra known
candidates from a previous DB export, but the primary source discovery is the
live search on Prehraj.to. Without any queue file, the pipeline takes episode
metadata from the backlog, searches Prehraj.to, and produces prepared plans.

Output:

```text
plans/prepared-episodes.jsonl
```

Each line is one episode plan with:

- `selected_source`: best source and evidence,
- `tested_sources`: every checked source,
- `upload_ready`: true only when the selected source has Czech audio evidence.

Run locally or from GitHub Actions:

```bash
python src/prepare_episode_sources.py --episode-limit 10
```

For stricter language confirmation:

```bash
WHISPER_LANGUAGE_CHECK=1 python src/prepare_episode_sources.py --use-whisper --episode-limit 3
```
