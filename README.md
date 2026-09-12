# podcast-mirror

Mirrors the RoguePod LiteCast audio from Acast into GitHub Releases and publishes
a static RSS feed on GitHub Pages with stable URLs and true byte lengths, **for
YouTube ingestion only**.

Nothing here touches Acast hosting or the public feed. Acast stays the origin;
this is a read-only mirror published alongside it. Total cost: $0 — no
third-party service, no credit card, no DNS change.

## The problem this solves

YouTube's RSS ingestion never gets as far as downloading the audio:

```
$ curl -I https://sphinx.acast.com/p/open/s/<show>/e/<episode>/media.mp3
HTTP/2 200
content-type: text/plain; charset=utf-8
content-length: 2
```

A cold `HEAD` against Acast's stitcher returns a 2-byte `text/plain` body; only a
`GET` follows the 302 to `stitcher2.acast.com` and returns real audio. Any
fetcher that probes with `HEAD` first sees a 2-byte text file and gives up. The
Patreon pass-through feed made this worse by dropping `enclosure @length` to `0`
from the 2026-04-29 episode onward.

There is a second, quieter problem: Acast's declared `@length` is wrong. Measured
on episode 50, the feed declares **44,834,272** bytes and the actual download is
**44,834,524** — 252 bytes out. That is why the pipeline records the on-disk byte
count and never copies `@length` from the source. (The bytes themselves are
deterministic — three separate downloads of the same episode were identical — so
there is no per-request ad stitching to worry about.)

The mirror fixes both: release assets are real objects, so `HEAD` returns `200`
with a correct `Content-Length` and `accept-ranges: bytes`, and every `@length`
in `feed.xml` is the true size of the asset it points at.

## Where things live

Everything is in one **public** GitHub repo (public keeps Actions minutes
unlimited and is required for Pages on the free plan):

| Thing | Where it lives | Public URL |
|---|---|---|
| Episode MP3s | Assets on the release tagged `audio` | `https://github.com/<owner>/<name>/releases/download/audio/<slug>-<digest>.mp3` |
| `feed.xml` | Committed to `main` | `https://<owner>.github.io/<name>/feed.xml` |
| `state/manifest.json` | Committed to `main` | — |

GitHub's docs say release assets have no total-size or bandwidth limit; the
per-file cap is 2 GB and episodes are ~45 MB. Verified on a real asset: `HEAD`
→ 302 → `200` with correct `content-length`, `accept-ranges: bytes`, and a `206`
with `Content-Range` on a range request.

**Content-type, resolved.** GitHub serves release downloads as
`application/octet-stream` with `content-disposition: attachment`, whatever
type was used at upload. This was the one open question before the first
rollout; it turned out not to matter — YouTube ingested the feed and produced
videos from octet-stream assets (2026-09-12), going by the
`<enclosure type="audio/mpeg">` attribute and the MP3 bytes. `verify` still
logs a `NOTE` line with the content-type it saw, for the record.

## How it works

```
Acast feed ──▶ diff against manifest ──▶ GET new enclosures (sequential)
                                            │
                              validate: audio/* · ≥1 MB · ID3/MPEG header
                                            │
                              upload as a release asset (delete-then-upload)
                                            │
                   state/manifest.json (written only after upload succeeds)
                                            │
                     feed.xml = source metadata + mirrored URLs and lengths
                                            │
                      Actions commits feed.xml + manifest, Pages publishes
```

- `state/manifest.json` is the source of truth for what has been mirrored. It
  records guid, source URL, asset key, true byte length, sha256, mirrored-at,
  and an archived copy of the item's XML. Living in git, it has free version
  history and no bootstrap problem.
- Because the manifest archives each item's XML, an episode Acast later drops
  from its feed **stays in the mirrored feed**, so YouTube never deletes the
  corresponding video.
- `feed.xml` is a deep copy of the Acast `<rss>` with the items replaced, so
  namespaced channel metadata — including `itunes:owner/itunes:email`, which
  YouTube's ownership verification reads — survives untouched. Only the
  `<enclosure>` is rewritten per item; `atom:link rel=self` points at the Pages
  URL and `itunes:new-feed-url` is stripped so YouTube can't be bounced back to
  Acast.
- An episode that fails to mirror is omitted from `feed.xml` entirely rather
  than published with a broken enclosure, and the run exits non-zero.
- The release is created automatically on first upload; nothing needs to exist
  in advance.

## Setup

### 1. Repo and Pages

1. Create a **public** repo (`roguepod-litecasst/roguepod-rss`) and push this
   directory to `main`. This directory is meant to be the root of its own
   repository — see the note under *Routine operation* if you nest it.
2. Settings → Pages → Source: **Deploy from a branch**, branch `main`, folder
   `/ (root)`. The feed URL is then
   `https://roguepod-litecasst.github.io/roguepod-rss/feed.xml`.
3. Settings → Actions → General → Workflow permissions: **Read and write**
   (the workflow declares `permissions: contents: write`, but the repo setting
   must allow it).

No secrets are needed. The workflow uses the built-in `GITHUB_TOKEN` for
release uploads and for pushing `feed.xml` and the manifest.

### 2. Environment (local runs only)

Inside Actions, `GITHUB_REPOSITORY` and `GITHUB_TOKEN` are provided
automatically and `PODCAST_FEED_URL` defaults in the workflow. For local runs,
copy `.env.example` to `.env`, fill it in, and load it:

```bash
cp .env.example .env
$EDITOR .env                       # GITHUB_TOKEN=$(gh auth token) works
set -a && source .env && set +a
```

`.env` is gitignored. The token is read from the environment only: never
hardcoded, never written to the manifest, never logged, and excluded from
`Config`'s `repr`.

### 3. Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt    # lxml only; the GitHub path is stdlib urllib
```

## Rollout

Staged deliberately so a problem with the host is caught on one episode
before the 2.4 GB commitment. (The first rollout is done; this is kept for
re-doing it against a new repo or a different host.)

### Stage 1 — one episode, end to end

```bash
python mirror.py --dry-run                 # see the plan, changes nothing
python mirror.py --max-new 1               # mirror the oldest unmirrored episode
git add feed.xml state/manifest.json && git commit -m "Mirror first episode" && git push
python mirror.py verify                    # after Pages has published
```

(Or trigger the workflow by hand: Actions → *Mirror podcast feed* → Run
workflow. It caps at 10 new episodes, commits, waits for Pages, and verifies.)

Then confirm the feed by eye — `curl -sI` a release asset URL, paste the Pages
URL into a feed validator — and submit it to YouTube. YouTube verifies
ownership by emailing the address in `itunes:owner` (`roguepodlitecast@gmail.com`,
copied verbatim from Acast — keep it reachable). Wait for one video to appear.
**This is the decision point.**

### Stage 2 — backfill, locally, not in Actions

```bash
python mirror.py --dry-run --backfill      # the plan
python mirror.py --backfill --keep-downloads
git add feed.xml state/manifest.json && git commit -m "Backfill" && git push
python mirror.py verify
```

All ~54 episodes, roughly 2.4 GB, downloaded sequentially with a 2 s delay. Do
this on your own machine: a hosted runner would hammer Acast from a shared
egress IP and risks the job timeout partway through.

`--keep-downloads` leaves the MP3s in `downloads/` (gitignored) so a later move
to different storage never means re-downloading from Acast.

It is safe to interrupt. The manifest is written atomically after each
successful upload, so re-running `--backfill` picks up exactly where it stopped
and re-downloads nothing. YouTube picks up the new items on its next poll and
creates videos over the following days.

### Stage 3 — hands off

`.github/workflows/mirror.yml` is already scheduled. Every 6 hours it diffs
Acast against the manifest, mirrors anything new, commits and pushes
`feed.xml` + `state/manifest.json`, waits for Pages to serve the new feed, and
runs `verify`.

### If GitHub stops working as a host

GitHub's release-asset policy is "no stated limit", not a contractual quota.
If that ever changes, switch `GitHubStorage` → `S3Storage` pointed at
Cloudflare R2's free tier
(10 GB, egress free, the `pub-*.r2.dev` URL needs no custom domain). Still $0;
needs a card on file. `pip install -r requirements-s3.txt` for boto3, set the
`STORAGE_*` / `R2_ACCOUNT_ID` variables in `.env.example`, and point
`AUDIO_PUBLIC_BASE_URL` at the R2 public URL. The MP3s kept by
`--keep-downloads` are re-uploaded; Acast is not touched again. Any other
S3-compatible provider works the same way via `STORAGE_ENDPOINT_URL`.

## Routine operation

The workflow: `permissions: contents: write`, no secrets, the 6-hour cron, a
`concurrency` group so two runs never mirror the same episode, and a
`workflow_dispatch` input for a dry run. After the mirror step it:

1. Commits `feed.xml` and `state/manifest.json` as `github-actions[bot]` and
   pushes — **even if the mirror step failed part-way**, because every episode
   that did upload is already in the manifest and committing it is what stops
   the next run from re-doing them. A push with `GITHUB_TOKEN` does not trigger
   another run.
2. Polls the Pages feed URL for up to 15 minutes until its bytes equal the
   committed `feed.xml` (Pages publishes asynchronously; without this, verify
   would assert against the previous run's feed).
3. Runs `python mirror.py verify`.

Dry runs skip all three. A scheduled run mirrors at most `--max-new` episodes
(default 10) so a surprise backlog can never blow the 45-minute job timeout; the
rest are picked up next run.

To point the feed at a different Acast URL, set a repository *variable*
`PODCAST_FEED_URL` (Settings → Secrets and variables → Actions → Variables).

> This directory is meant to be the root of its own repository. If you nest it
> inside a larger repo, move `.github/workflows/mirror.yml` to that repo's root
> `.github/workflows/`, add a `working-directory` to the run steps, and update
> the Pages path and the `FEED_URL` in the wait step.

## Commands

```bash
python mirror.py                       # mirror new episodes, rewrite feed.xml
python mirror.py --dry-run             # report only; downloads and writes nothing
python mirror.py --backfill            # mirror every episode, no per-run cap
python mirror.py --keep-downloads      # don't delete MP3s after upload
python mirror.py --delay 5             # seconds between downloads (default 2)
python mirror.py --max-new 3           # cap new episodes this run (0 = no cap)
python mirror.py verify                # assert the live mirror is correct
python mirror.py verify --sample 5     # HEAD more episodes (default 3)
```

Common flags on both subcommands: `--feed-url`, `--repo`, `--release-tag`,
`--audio-base-url`, `--feed-base-url`, `--state-dir`, `-v`. Each defaults to the
corresponding environment variable (`PODCAST_FEED_URL`, `GITHUB_REPOSITORY`,
`RELEASE_TAG`, `AUDIO_PUBLIC_BASE_URL`, `FEED_PUBLIC_BASE_URL`).

Exit codes: `0` success · `1` at least one episode failed to mirror ·
`2` configuration or source-feed error · `130` interrupted.

## Verification

`python mirror.py verify` asserts, against the live public URLs:

1. `HEAD` on the three newest mirrored episodes returns `200`, a content-type
   of `audio/mpeg` **or** `application/octet-stream` (logged as a `NOTE`;
   `text/plain` fails), a `Content-Length` matching the mirrored byte length,
   and `accept-ranges: bytes`. The `HEAD` is kept as a `HEAD` across GitHub's
   302 (Python's default redirect handler would turn it into a `GET`).
2. A range request (`Range: bytes=0-1023`) returns `206` with the correct
   `Content-Range` and a 1024-byte body.
3. `feed.xml` fetches as `application/rss+xml`, `application/xml` or `text/xml`
   (Pages serves `.xml` as `application/xml`; `text/html` fails), parses as
   RSS 2.0, still carries `<itunes:owner><itunes:email>`, has one item per
   manifest entry, and **every** enclosure `@length` equals the actual asset
   size on the release.

Any failure exits non-zero with the failing assertions listed.

Manual checks not covered by code:

1. `curl -sI <a release asset URL>` — confirm `content-length` and
   `accept-ranges`, and note the `content-type`.
2. Paste the Pages feed URL into a feed validator.
3. The only true end-to-end test is YouTube accepting the feed and producing a
   video — which is what Stage 1 is for.

## Tests

```bash
python -m unittest discover -s tests -t .
```

63 offline tests run the real pipeline against a local HTTP origin and a fake
GitHub API (`tests/support.py`) that serves downloads as
`application/octet-stream` behind a 302, like the real thing. Covered: the
sphinx placeholder failure mode, the too-small body, a non-MP3 body,
dropped-episode retention, a second run being a no-op, release-asset
pagination and delete-then-reupload with 5xx retries, `--keep-downloads`, URL
derivation from the repo name, and `verify` end to end against the fake.

## Pointing YouTube at the feed

Give YouTube the Pages URL,
`https://roguepod-litecasst.github.io/roguepod-rss/feed.xml`. Ownership
verification uses `<itunes:owner><itunes:email>`, which is copied verbatim from
the Acast feed (`roguepodlitecast@gmail.com`) — keep that address reachable.

## Recovery notes

- **Lost manifest.** It's in git; `git checkout state/manifest.json` from any
  earlier commit restores it. If it's really gone, `--backfill` re-mirrors
  everything. Keys are derived from the guid, so re-mirroring overwrites the
  same assets and every URL already published stays valid.
- **Re-mirroring an episode.** Once an episode is in the manifest it is never
  re-downloaded; a same-named asset is deleted before re-upload, so a URL never
  points at a half-uploaded file. Downloads were verified byte-identical across
  requests, but the mirror still never trusts that — the length in the feed is
  always the size of the bytes actually uploaded.
- **Moving storage.** The MP3s kept by `--keep-downloads` plus the manifest are
  everything needed to re-upload elsewhere without touching Acast.
