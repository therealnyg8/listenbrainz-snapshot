# ListenBrainz snapshot for therealnyg8

A public, static JSON source for album recommendations. GitHub Actions refreshes
`therealnyg8.json` daily at 09:23 UTC, on manual dispatch, and when the pipeline changes.
Scheduled runs can be delayed by GitHub.

## Snapshot contents

- Schema version, generation timestamp, source URLs, and explicit coverage metadata.
- Up to 90 days of recent listens, newest first, with artist, track, album, UTC
  timestamp, available MusicBrainz identifiers, and submission clients.
- Artist and album rankings for the last 7, 30, and 90 days, including listen counts,
  unique tracks per album, and the latest listen to each album.
- Exact duplicate imports at the same timestamp are merged. Repeated plays at
  different times remain. Album counts are track listens, not whole-album plays.
- A maximum of ten 1,000-listen API pages per run. If the bound is reached,
  `coverage.complete` is false; aggregates cover fetched data only.

No ListenBrainz token, personal GitHub token, Python packages, or paid service is
required. The workflow uses the repository-scoped, temporary GitHub Actions token
to commit its output. API calls identify this repository in the User-Agent,
respect rate limits, and retry temporary failures. Failures preserve the prior
snapshot; consumers should check `generated_at_utc` for freshness.

The current file has a rolling window; older committed snapshots remain in public
Git history. Only already-public listening metadata is included.

## Run and verify

```sh
python3 -m unittest -v
python3 snapshot.py
```

In GitHub: Actions → Refresh ListenBrainz snapshot → Run workflow.
After a successful run, open `therealnyg8.json` and choose Raw for the stable URL.
Anonymous HTTP access and a successful read by ChatGPT's web reader are separate
checks. Neither guarantees availability to every future scheduled ChatGPT run.

GitHub may disable schedules after 60 days without repository activity. Regular
successful snapshot commits keep this repository active; check Actions if the
snapshot becomes stale. This repository does not modify the Monday recommendation
automation.
