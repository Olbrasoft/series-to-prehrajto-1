# Discovered episode sources

`sources/discovered-episode-sources.jsonl.gz` is the durable source catalog built
while preparing upload manifests. It records every candidate source we considered
for each exported episode, not only the one selected for upload.

`exports/video-source-import.jsonl.gz` is the filtered import feed for production
DB enrichment. It intentionally excludes permanently dead sources and keeps rows
that are new or have useful language/quality evidence.

Source quality tiers:

- `preferred`: Czech audio signal and either 1080p+ or at least 300 MB.
- `acceptable`: Czech audio signal but below the preferred quality threshold.
- `rejected`: not suitable for upload.
- `dead`: permanently failed during upload/resolve.
- `uploaded`: already uploaded successfully.

The production import must remain a separate approved step. This repository only
prepares import-ready evidence.
