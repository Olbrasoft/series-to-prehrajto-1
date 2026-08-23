# Language Audit

The upload backlog is not enough for series. Some episode sources look Czech
from the page title, but metadata or audio proves otherwise. This repository
therefore stores an audit trail that can later be imported back into the
production DB.

## Files

```text
backlog/language-audit-queue.jsonl.gz  source rows exported from production
audits/language-audit.jsonl            append-only evidence and verdicts
```

Each audit row includes:

- source identity: `provider`, `source_id`, `external_id`,
- episode identity: `series_slug`, `episode_id`, `season`, `episode`,
- DB signals: `db_lang_class`, `db_audio_lang`,
- title and metadata signals,
- optional Whisper result,
- final verdict: `CZ_AUDIO`, `SK_AUDIO`, `NOT_CZ_AUDIO`, `CZ_SUBTITLES_ONLY`,
  `PROBABLE_CZ_AUDIO`, or `UNKNOWN`.

## Export

Always use a read-only production DB connection:

```bash
DATABASE_URL='postgres://cr:***@127.0.0.1:15432/cr?options=-c default_transaction_read_only=on' \
  python src/export_language_audit_queue.py --limit 500
```

Specific example:

```bash
DATABASE_URL='postgres://cr:***@127.0.0.1:15432/cr?options=-c default_transaction_read_only=on' \
  python src/export_language_audit_queue.py \
  --out backlog/language-audit-hvezdne-mestecko.jsonl.gz \
  --series-slug hvezdne-mestecko --season 1 --episode 2
```

## Run

Fast metadata/title pass:

```bash
python src/audit_language_sources.py --limit 100
```

Whisper pass for supported direct-stream providers:

```bash
WHISPER_LANGUAGE_CHECK=1 python src/audit_language_sources.py --use-whisper --limit 20
```

SKTorrent rows without direct stream metadata are still recorded as unresolved
evidence. They remain useful because they identify missing provider resolver
coverage and keep the source IDs importable later.
