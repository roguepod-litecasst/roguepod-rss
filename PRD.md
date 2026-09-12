# PRD: Free GitHub-hosted RSS mirror for YouTube ingestion

## Context

RoguePod LiteCast is hosted on Acast, which won't let us hand the feed to YouTube.
The fix is a second RSS feed we control that mirrors the Acast one, which YouTube
can ingest to auto-create a video per episode.

A previous attempt (`~/git/rss-mirror`, never committed anywhere) built a working
pipeline but concluded it needed paid object storage (Bunny.net, $1/mo) plus a DNS
change, because Cloudflare R2 wanted the whole `roguepod.show` zone on Cloudflare
nameservers. That conclusion was wrong about one thing: **GitHub will host all of
this for free.**

### What was verified before planning

- **Acast enclosures are genuinely unusable as-is.** `HEAD` on
  `sphinx.acast.com/.../media.mp3` returns `200 text/plain, content-length: 2`.
  A `GET` follows a 302 to `stitcher2.acast.com` and returns real audio. Any
  fetcher that probes with `HEAD` first sees a 2-byte text file. This is why the
  audio must be re-hosted rather than linked through.
- **Acast's declared `length` is wrong.** Feed says `44834272`; the actual body is
  `44834524` — 252 bytes out. The mirror must publish measured byte counts.
- **There is no dynamic ad-stitching to worry about.** Three separate requests for
  the same episode all returned exactly `44834524` bytes, so downloads are
  deterministic and YouTube's "podcasts cannot contain advertisements" policy
  isn't in play.
- **GitHub Releases is a sanctioned, unmetered host.** GitHub's own docs: *"We
  don't limit the total size of the binary files in the release or the bandwidth
  used to deliver them."* Verified on a real asset: `HEAD` → 302 → `200` with
  correct `content-length`, `accept-ranges: bytes`, and `206 Content-Range` on a
  range request. Per-file cap is the plan's Git LFS limit (2 GB); episodes are ~45 MB.
- **One known unknown.** GitHub serves release downloads as
  `application/octet-stream` with `content-disposition: attachment`, regardless of
  the type used at upload. Confirmed against an asset whose stored `content_type`
  is `application/json` — it still downloads as octet-stream. The
  `<enclosure type="audio/mpeg">` attribute is still correct, and MP3 bytes are
  trivially sniffable, so this very likely doesn't matter. **The rollout below
  tests it with one episode before committing to the 2.4 GB backfill.**

### Decisions made

- All 54 episodes get backfilled.
- Audio lives in GitHub Releases. Cloudflare R2 is the documented fallback if the
  content-type turns out to matter.
- Feed URL is the GitHub Pages subpath — no DNS changes, so `roguepod.show`'s
  website, Google email, MX and SPF records are untouched.

## Target architecture

Everything in one new **public** GitHub repo, `roguepod-litecasst/podcast-mirror`
(public keeps Actions minutes unlimited and is required for Pages on the free plan):

| Thing | Where it lives | Public URL |
|---|---|---|
| Episode MP3s | Release assets on tag `audio` | `github.com/roguepod-litecasst/podcast-mirror/releases/download/audio/<slug>-<digest>.mp3` |
| `feed.xml` | Committed to the repo | `roguepod-litecasst.github.io/podcast-mirror/feed.xml` |
| `state/manifest.json` | Committed to the repo | — |

Total cost: **$0.** No credit card, no DNS change, no third-party service.

The manifest moving into git (it was an R2 object before) is a straight
simplification: free version history, and no bootstrap problem where the manifest
lives in the store it's meant to describe.

## What to reuse

`~/git/rss-mirror/podcast_mirror/` is sound, tested (25 offline tests), and solves
several non-obvious problems. Copy it into the new repo and change only the
storage layer. These files move **verbatim**:

- `source.py` — parses Acast's feed with lxml, keeping the original element tree
  so namespaced channel metadata (`itunes:owner/itunes:email`, which is what
  YouTube's ownership verification reads) survives untouched.
- `audio.py` — streams each enclosure with `GET` only, never `HEAD`, and gates on
  three checks: `Content-Type: audio/*`, a 1 MB floor, and an ID3/MPEG frame
  header. `looks_like_mp3()` is the placeholder-response detector.
- `feedgen.py` — deep-copies the source `<rss>`, drops items, re-appends one per
  mirrored episode with only the `<enclosure>` rewritten. Also rewrites the
  `atom:link rel=self` and strips `itunes:new-feed-url` so YouTube can't be
  bounced back to Acast.
- `manifest.py` — archives each item's XML, so an episode Acast later drops stays
  in our feed and YouTube never deletes the video. `audio_key()` derives a stable
  key from the guid.
- `pipeline.py`, `errors.py`, `tests/` — unchanged.

## Changes to make

### 1. `podcast_mirror/storage.py` — add `GitHubStorage`

Add one class implementing the existing `Storage` protocol, routing by key prefix
so **`pipeline.py` and `manifest.py` need no changes at all**:

- key starts with `audio/` → GitHub Release asset. `put_file` uploads via the
  releases API (asset name = `os.path.basename(key)`, since release assets are a
  flat namespace); `head` reads `GET /repos/{owner}/{repo}/releases/tags/{tag}`
  and matches on asset name, returning `{size, content_type}`; re-uploads delete
  the existing asset first (the API rejects duplicate names).
- any other key (`feed.xml`, `manifest.json`) → plain file in the local repo
  working tree. The workflow commits and pushes them.

Keep `S3Storage` in the file. It is the R2 fallback if the content-type gamble
loses, and deleting it would mean rewriting it.

> **Done (2026-09-11).** `GitHubStorage(repo, token, *, release_tag, audio_prefix,
> local_dir, api_base, upload_base, ...)` plus `GitHubStorage.from_config(cfg)`,
> stdlib-only. Asset listing uses the paginated `/releases/{id}/assets` endpoint;
> uploads stream the file, delete any same-named asset first, and retry 5xx
> three times. Local keys are written atomically (tempfile + `os.replace`) and
> refuse to escape `local_dir`. New `StorageError` in `errors.py`. Tests:
> `tests/test_github_storage.py` (16 tests) against a `FakeGitHub` in
> `tests/support.py` that serves downloads as `application/octet-stream` +
> `content-disposition: attachment`, like the real thing. `.gitignore` now
> tracks `state/manifest.json`. Wiring as the pipeline default and the config
> fields `from_config` reads are task 2; that task should also set
> `manifest_key = "state/manifest.json"` so `Manifest.save`'s local write and
> the storage write land on the same file.

### 2. `podcast_mirror/config.py` — two base URLs, no credentials

- Drop the S3 fields and `endpoint_url` derivation from the required path.
- Add `github_repo` (`$GITHUB_REPOSITORY`), `github_token` (`$GITHUB_TOKEN`),
  `release_tag` (default `audio`).
- Split `base_url` into `audio_base_url` and `feed_base_url`; make
  `public_url(key)` route on the `audio/` prefix and `feed_public_url` use the
  Pages URL. This is the one place the two-host split is expressed.

> **Done (2026-09-11).** `Config` now takes `github_repo`, `github_token`
> (excluded from `repr`), `release_tag`, `audio_base_url`, `feed_base_url`;
> the two base URLs derive from the repo in `__post_init__`
> (`github.com/<repo>/releases/download/<tag>` and
> `<owner>.github.io/<name>`) and can be overridden via
> `AUDIO_PUBLIC_BASE_URL` / `FEED_PUBLIC_BASE_URL` or `--audio-base-url` /
> `--feed-base-url` — that override is how the R2 fallback would be pointed.
> `public_url()` routes audio keys to the release by basename and everything
> else to Pages at its path; `key_for_url()` is the inverse, used by
> `verify.py` instead of string-slicing `base_url`. `manifest_key` defaults
> to `state/manifest.json`. `pipeline.py` and `verify.py` default to
> `GitHubStorage.from_config(cfg)`. CLI: `--bucket`/`--base-url` replaced by
> `--repo`, `--release-tag`, `--audio-base-url`, `--feed-base-url`.
> `S3Storage` config (`bucket`, endpoint, S3 creds) kept as optional fields;
> `require_credentials` now also names `STORAGE_BUCKET`. `.env.example`
> rewritten. Tests: 57 pass (10 new in `PublicUrlConfigTest`). Also fixed a
> task-1 bug in `FakeGitHub`'s download route that 404'd every redirect.

### 3. `podcast_mirror/verify.py` — accept octet-stream for audio

The existing audio assertion requires `audio/mpeg`. Relax it to accept
`audio/mpeg` **or** `application/octet-stream`, and log loudly which one came
back — that log line is the answer to the open question. Keep every other
assertion as-is: `content-length` matching the manifest, `accept-ranges: bytes`,
the `206`/`Content-Range` range check, and the feed.xml checks (parses as RSS,
still has `itunes:owner/itunes:email`, one item per manifest entry, every
`@length` correct).

> **Done (2026-09-11).** `verify.AUDIO_CONTENT_TYPES = ("audio/mpeg",
> "application/octet-stream")` gates both the HEAD check and the per-item
> `storage.head()` check. The type seen on each sampled HEAD is recorded in
> `Checks.audio_content_types` and logged as a `NOTE` line — INFO for
> `audio/mpeg`, WARNING for octet-stream — so it stands out in the Actions
> log. `text/plain` still fails. Found and fixed while testing: the stdlib
> redirect handler rewrites a `HEAD` as a `GET` when following the 302 every
> release URL returns, so verify was doing full-body GETs it then discarded;
> `_KeepMethodRedirectHandler` keeps it a HEAD. Tests: new
> `VerifyOnGitHubTest` runs verify end to end against `FakeGitHub`
> (octet-stream, 302, HEAD preserved) and `VerifyTest` gains a `text/plain`
> rejection case. 60 tests pass. Note for task 6 / Stage 1: the feed check
> still requires `application/rss+xml`; GitHub Pages serves `.xml` as
> `application/xml`, so that assertion will need relaxing when the workflow
> is wired up.

### 4. `podcast_mirror/pipeline.py` — one small addition

Add a `--keep-downloads` flag that skips the `os.unlink(result.path)` after
upload. The 2.4 GB backfill should keep local copies so that switching to R2
later never means re-downloading from Acast. Everything else stays.

> **Done (2026-09-11).** `run(cfg, ..., keep_downloads=False)` threads through
> to `_mirror_one`, which logs `kept local copy at <path>` instead of
> unlinking. `cli.py` adds `--keep-downloads` to the `mirror` subcommand.
> Tests: `test_downloads_are_deleted_after_upload_by_default` and
> `test_keep_downloads_leaves_verified_mp3s_on_disk` (the kept file's size and
> sha256 match the manifest entry). 62 tests pass.

### 5. `requirements.txt`

Drop `boto3` to an extra (only needed for the R2 fallback); keep `lxml`.

> **Done (2026-09-11).** `requirements.txt` is now `lxml` only;
> `requirements-s3.txt` (`-r requirements.txt` + `boto3>=1.34`) is the extra
> for the R2 fallback. `boto3` was already imported lazily inside
> `S3Storage.__init__`; a missing install now raises `StorageError` naming
> `requirements-s3.txt` instead of a bare `ImportError`. The workflow's
> `pip install -r requirements.txt` line needs no change. 62 tests pass.

### 6. `.github/workflows/mirror.yml`

Rewrite from the existing one: `permissions: contents: write`, no secrets beyond
the built-in `GITHUB_TOKEN`, and after the mirror step commit `feed.xml` and
`state/manifest.json` back to the branch. Keep the 6-hour cron, the
`concurrency` group, the `workflow_dispatch` dry-run input, and the verify step.
Pages set to "deploy from branch", so the push publishes.

> **Done (2026-09-11).** `permissions: contents: write`; the only credential
> is `${{ github.token }}` exported as `GITHUB_TOKEN`. `PODCAST_FEED_URL`
> defaults to the public Acast URL in the workflow itself (overridable via a
> repo *variable*, not a secret). Steps after the mirror: commit
> `feed.xml` + `state/manifest.json` as `github-actions[bot]` and push
> (skipped when nothing changed; runs even if the mirror step failed
> part-way so already-uploaded episodes aren't re-mirrored next run); poll
> the Pages feed URL for up to 5 minutes until its bytes equal the committed
> `feed.xml` (Pages publishes asynchronously after the push, and verify
> would otherwise assert against the previous run's feed); then
> `mirror.py verify`. Cron, concurrency group and the dry-run dispatch input
> are unchanged; dry runs skip all three post-mirror steps. Also resolved
> the task-3 flag: `verify.FEED_CONTENT_TYPES` accepts `application/rss+xml`,
> `application/xml` or `text/xml` (Pages serves `.xml` as `application/xml`);
> `text/html` still fails. `VerifyOnGitHubTest` now serves the feed as
> `application/xml; charset=utf-8` like Pages does, plus a new HTML-rejection
> case. 63 tests pass. Not verifiable offline: the workflow has to run once
> in the real repo (Stage 1) — that's also when Pages must be enabled with
> source "deploy from branch", root `/`.

### 7. `README.md`

Rewrite. The current one is largely Bunny.net setup and DNS instructions that no
longer apply. Keep the "problem this solves" section — the HEAD/length findings
are still the reason this exists.

> **Done (2026-09-11).** README rewritten for GitHub hosting: "problem this
> solves" kept (HEAD placeholder, wrong `@length`), with the old "Acast
> stitches ads per request" claim corrected to the verified finding that
> downloads are byte-identical. New sections: where things live (the
> release/Pages table), the octet-stream known unknown and where `verify`
> logs the answer, setup (public repo, Pages "deploy from branch", Actions
> workflow permissions read+write, no secrets), the three rollout stages
> with exact commands, the R2 fallback path, what the workflow does after
> the mirror step, full command/flag list, the relaxed verify assertions,
> the 63-test suite, and recovery notes updated for a git-tracked manifest.
> All Bunny.net/DNS/S3-secrets material removed. No code changed; 63 tests
> pass.

## Rollout

Staged deliberately, so the content-type unknown is answered before the 2.4 GB
commitment.

**Stage 1 — one episode, end to end.**
Create the repo, enable Pages, mirror only the newest episode
(`--max-new 1`), confirm `feed.xml` is live and well-formed, then submit it to
YouTube. YouTube verifies ownership by emailing `roguepodlitecast@gmail.com` (the
address already in Acast's `itunes:owner`) with a code. Wait for one video to
appear. This is the decision point.

**Stage 2 — backfill, locally.**
`python mirror.py --backfill --keep-downloads`, run from this machine, not
Actions: ~2.4 GB over 54 sequential downloads with a 2s delay. Safe to interrupt
— the manifest is written atomically after each upload, so re-running resumes
exactly where it stopped. Then commit the feed and manifest. YouTube picks up the
53 new items on its next poll and creates the videos over the following days.

**Stage 3 — hands off.**
Enable the schedule. Every 6 hours the workflow diffs Acast against the manifest,
mirrors anything new (capped at 10 per run so a surprise backlog can't blow the
job timeout), republishes, and verifies.

**If Stage 1 fails on content-type:** switch `GitHubStorage` → `S3Storage`
pointed at Cloudflare R2's free tier (10 GB storage, egress free, `pub-*.r2.dev`
public URL needs no custom domain). Still $0, needs a card on file. The local
MP3s from `--keep-downloads` are re-uploaded; Acast is not touched again.

## Verification

```bash
# Offline — the existing 25-test suite, plus new GitHubStorage tests against a fake API
python -m unittest discover -s tests -t .

# Dry run — reports the plan, downloads and writes nothing
python mirror.py --dry-run --backfill

# Live assertions against the published URLs
python mirror.py verify
```

`verify` is the real end-to-end check. It asserts, over HTTP against the live
public URLs, that the newest mirrored episodes return `200` with a
`Content-Length` matching the manifest and `accept-ranges: bytes`, that a range
request returns `206` with the right `Content-Range`, and that `feed.xml` parses
as RSS 2.0 with `itunes:owner/itunes:email` intact and every `@length` equal to
the real asset size.

Manual checks not covered by code:

1. `curl -sI <a release asset URL>` — confirm `content-length` and
   `accept-ranges`, and note the `content-type`.
2. Paste the Pages feed URL into a feed validator to confirm it parses as a
   podcast feed.
3. The only true end-to-end test is YouTube itself accepting the feed and
   producing a video — which is exactly what Stage 1 is for.

## Notes

- `~/git` is itself one big git repo with every project untracked inside it. The
  mirror needs its own repository with its own Actions and Pages, so the work goes
  in `~/git/rss-mirror-2` and gets pushed to a fresh GitHub repo — not committed
  into the `~/git` tree.
- Nothing here touches Acast. It stays the origin and the public feed; this is a
  read-only mirror published alongside it, for YouTube only.
