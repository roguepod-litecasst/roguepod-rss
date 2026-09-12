# podcast-mirror

Mirrors the RoguePod LiteCast audio from Acast into Cloudflare R2 and publishes a
static RSS feed with stable URLs and true byte lengths, **for YouTube ingestion
only**.

Nothing here touches Acast hosting or the public feed. Acast stays the origin;
this is a read-only mirror published alongside it.

## The problem this solves

YouTube's RSS ingestion never gets as far as downloading the audio:

```
$ curl -I https://sphinx.acast.com/p/open/s/<show>/e/<episode>/media.mp3
HTTP/2 200
content-type: text/plain; charset=utf-8
content-length: 2
```

A cold `HEAD` against Acast's stitcher returns a 2-byte `text/plain` body, so
YouTube's fetcher gives up before it ever issues a `GET`. The Patreon
pass-through feed made this worse by dropping `enclosure @length` to `0` from the
2026-04-29 episode onward.

There is a second, quieter problem: Acast stitches ads per request, so the
`@length` in the source feed is only ever an estimate. Measured on episode 50,
the feed declares **44,834,272** bytes and the actual download is **44,834,524** —
252 bytes out. That is why the pipeline records the on-disk byte count and never
copies `@length` from the source.

The mirror fixes both: R2 serves real objects, so `HEAD` returns
`200 audio/mpeg` with a correct `Content-Length` and `accept-ranges: bytes`, and
every `@length` in `feed.xml` is the true size of the object it points at.

## How it works

```
Acast feed ──▶ diff against manifest ──▶ GET new enclosures (sequential)
                                            │
                              validate: audio/* · ≥1 MB · ID3/MPEG header
                                            │
                                     upload to R2 (audio/mpeg)
                                            │
                          manifest.json (written only after upload succeeds)
                                            │
                     feed.xml = source metadata + mirrored URLs and lengths
```

- `manifest.json` in R2 is the source of truth for what has been mirrored. It
  records guid, source URL, R2 key, true byte length, sha256, mirrored-at, and an
  archived copy of the item's XML.
- Because the manifest archives each item's XML, an episode Acast later drops
  from its feed **stays in the mirrored feed**, so YouTube never deletes the
  corresponding video.
- An episode that fails to mirror is omitted from `feed.xml` entirely rather than
  published with a broken enclosure, and the run exits non-zero.

## Setup

### 1. Bunny.net storage

Bunny is used rather than Cloudflare R2 because R2 only accepts a custom domain
if the *entire* domain's nameservers are on Cloudflare. `roguepod.show` keeps its
DNS at Porkbun and routes email through Google, and migrating that zone to point
one subdomain at a bucket is not a trade worth making. Bunny accepts a plain
CNAME from any DNS host and issues a free auto-renewing certificate for it.

Cost is the $1/month account minimum: ~3 GB of storage is about $0.03/month and
YouTube's one-time fetch of the catalogue about the same.

1. Create a bunny.net account and fund it.
2. **Storage → Add Storage Zone.**
   - Name it `roguepod-mirror`. This name is *also* the bucket name and the S3
     access key id.
   - Main region: Los Angeles (`la`) is the closest S3-enabled region.
   - **Tick "S3-compatible API" before creating the zone.** It cannot be enabled
     afterwards — the zone would have to be deleted and recreated.
3. Storage zone → **Access** tab. The Password shown there is the S3 secret
   access key.
4. **CDN → Add Pull Zone**, with the storage zone as its origin. This is what
   serves the files publicly; the storage zone alone is not public.
5. Pull zone → **Hostnames** → add `mirror.roguepod.show`.
6. At **Porkbun**, add one record — nothing else changes:

   | Type | Host | Answer |
   |---|---|---|
   | CNAME | `mirror` | `<pull-zone-name>.b-cdn.net` |

   The apex `A`/`AAAA` records, `www`, `MX` and the SPF/verification `TXT`
   records are untouched, so the website and email are unaffected.
7. Back in the pull zone hostname settings, generate the free Let's Encrypt
   certificate for `mirror.roguepod.show`.

### 2. Any other S3 provider

The storage layer is plain S3, so Backblaze, Wasabi, AWS or R2 all work. Set
`STORAGE_ENDPOINT_URL` to that provider's endpoint (for R2, set `R2_ACCOUNT_ID`
instead and the endpoint is derived). `STORAGE_REGION` is the signing region;
`auto` suits R2, and other providers want their own region code.

### 3. Environment

Copy `.env.example` to `.env`, fill it in, and load it:

```bash
cp .env.example .env
$EDITOR .env
set -a && source .env && set +a
```

`.env` is gitignored. Credentials are read from the environment only: never
hardcoded, never written to the manifest, and never logged (botocore's own
logging is pinned to WARNING so it cannot echo request metadata).

### 4. Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Run the initial backfill locally — not in Actions

The first run downloads all ~54 episodes, roughly **3 GB**. Do this on your own
machine. A GitHub-hosted runner would be slow, would hammer Acast from a shared
egress IP, and risks hitting the job timeout partway through.

```bash
python mirror.py --dry-run --backfill     # see the plan first, changes nothing
python mirror.py --backfill               # the real thing; ~3 GB, sequential
```

Episodes are streamed one at a time and deleted from disk after upload, so the
backfill needs only ~100 MB of free disk despite transferring ~3 GB.

It is safe to interrupt. The manifest is written atomically after each successful
upload, so re-running `--backfill` picks up exactly where it stopped and
re-downloads nothing.

Then verify against the live URLs:

```bash
python mirror.py verify
```

## Routine operation

After the backfill, GitHub Actions takes over: `.github/workflows/mirror.yml`
runs every 6 hours and on `workflow_dispatch`. Add all six environment variables
above as repository secrets (Settings → Secrets and variables → Actions).

A scheduled run mirrors at most `--max-new` episodes (default 10) so a surprise
backlog can never turn into a multi-gigabyte Actions job; the rest are picked up
by the next run. The workflow verifies the published mirror after every run.

> This directory is meant to be the root of its own repository. If you nest it
> inside a larger repo, move `.github/workflows/mirror.yml` to that repo's root
> `.github/workflows/` and add a `working-directory` to the run steps.

## Commands

```bash
python mirror.py                  # mirror new episodes, republish feed.xml
python mirror.py --dry-run        # report only; downloads and writes nothing
python mirror.py --backfill       # mirror every episode, no per-run cap
python mirror.py --delay 5        # seconds between downloads (default 2)
python mirror.py --max-new 3      # cap new episodes this run
python mirror.py verify           # assert the live mirror is correct
```

Exit codes: `0` success · `1` at least one episode failed to mirror ·
`2` configuration or source-feed error · `130` interrupted.

## Verification

`python mirror.py verify` asserts, against the live public URLs:

1. `HEAD` on the three newest mirrored episodes returns `200`, `audio/mpeg`, a
   `Content-Length` matching the mirrored byte length, and `accept-ranges: bytes`.
2. A range request (`Range: bytes=0-1023`) returns `206` with the correct
   `Content-Range` and a 1024-byte body.
3. `feed.xml` fetches as `application/rss+xml`, parses as RSS 2.0, still carries
   `<itunes:owner><itunes:email>`, has one item per manifest entry, and **every**
   enclosure `@length` equals the actual object size in R2.

Any failure exits non-zero with the failing assertions listed.

## Tests

```bash
python -m unittest discover -s tests -t .
```

25 offline tests run the real pipeline against an in-memory bucket and a local
HTTP origin, including the sphinx placeholder failure mode, the too-small body,
a non-MP3 body, dropped-episode retention, and a second run being a no-op.

## Pointing YouTube at the feed

Give YouTube `<STORAGE_PUBLIC_BASE_URL>/feed.xml` (`https://mirror.roguepod.show/feed.xml`). Ownership verification uses
`<itunes:owner><itunes:email>`, which is copied verbatim from the Acast feed
(`roguepodlitecast@gmail.com`) — keep that address reachable.

## Recovery notes

- **Lost manifest.** A copy is cached at `state/manifest.json` after every write;
  upload it back to R2 as `manifest.json`. If both are gone, `--backfill`
  re-mirrors everything. Keys are derived from the guid, so re-mirroring
  overwrites the same objects and every URL already published stays valid.
- **Re-mirroring changes the bytes.** Acast stitches ads per request, so a
  re-download of an episode is not byte-identical to the first one. Once an
  episode is in the manifest it is never re-downloaded, which is what keeps
  lengths stable.
