# Ops Monitoring

The pipeline has four moving parts:

1. source preparation: `plans/prepared-episodes.jsonl`,
2. Gemma descriptions: `plans/descriptions.jsonl`,
3. uploads: `state/uploaded.json`,
4. language evidence: `audits/language-audit.jsonl`.

`src/ops_status.py` summarizes all of them into:

```text
reports/ops-status.json
```

It reports:

- running GitHub workflows,
- uploaded episodes without prepared Gemma descriptions,
- backlog episodes without source preparation,
- backlog episodes without episode descriptions,
- prepared episodes that are not upload-ready,
- language verdict counts.

Future uploads run with `REQUIRE_PREPARED_DESCRIPTIONS=1`, so a missing Gemma
description blocks that episode instead of uploading with copied site text.
