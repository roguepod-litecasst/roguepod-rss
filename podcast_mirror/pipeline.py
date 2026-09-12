"""Orchestration: diff the feed, mirror what is new, republish feed.xml."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from .audio import download_episode
from .errors import DownloadError, MirrorError
from .feedgen import build_feed
from .manifest import Episode, Manifest, audio_key
from .source import SourceItem, load_source
from .storage import GitHubStorage

log = logging.getLogger(__name__)


@dataclass
class RunResult:
    mirrored: List[str]
    failed: List[str]
    deferred: List[str]
    feed_items: int
    feed_uploaded: bool
    manifest_uploaded: bool

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0


def _mirror_one(item: SourceItem, cfg, storage, manifest: Manifest,
                *, keep_downloads: bool = False) -> Episode:
    """Download, validate, upload, and record one episode.

    The manifest is written only after the audio object is in storage, so an
    interrupted run never claims an episode it did not actually mirror.

    With ``keep_downloads`` the validated MP3 stays in ``cfg.download_dir``
    after upload, so a later move to different storage can re-upload from
    disk instead of pulling ~2.4 GB from Acast again.
    """
    key = audio_key(cfg, item.guid)
    dest = os.path.join(cfg.download_dir, os.path.basename(key))
    log.info("Mirroring %r (guid=%s)", item.title, item.guid)
    log.info("  source: %s", item.enclosure_url)

    result = download_episode(item.enclosure_url, dest, item.title, cfg)
    if item.declared_length and item.declared_length.isdigit():
        declared = int(item.declared_length)
        if declared != result.length:
            # Expected: Acast stitches ads per request, so the feed's @length
            # is only ever an estimate. We publish the real one.
            log.info(
                "  feed declared %s bytes, actual %s (using actual)",
                f"{declared:,}", f"{result.length:,}",
            )

    log.info("  uploading %s -> %s", key, cfg.public_url(key))
    storage.put_file(result.path, key, "audio/mpeg")

    head = storage.head(key)
    if head is None or head["size"] != result.length:
        got = "missing" if head is None else f"{head['size']} bytes"
        raise DownloadError(item.title, f"post-upload check failed: stored object is {got}")

    episode = Episode(
        guid=item.guid,
        title=item.title,
        source_url=item.enclosure_url,
        key=key,
        length=result.length,
        sha256=result.sha256,
        mirrored_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        pub_date=item.pub_date,
        pub_ts=item.pub_ts,
        item_xml=item.item_xml,
    )
    manifest.add(episode)
    manifest.save(storage, cfg)
    log.info("  recorded in manifest (%s bytes, sha256 %s...)",
             f"{result.length:,}", result.sha256[:12])

    if keep_downloads:
        log.info("  kept local copy at %s", result.path)
    else:
        try:
            os.unlink(result.path)
        except OSError:
            pass
    return episode


def run(cfg, *, dry_run: bool = False, backfill: bool = False,
        keep_downloads: bool = False, storage=None) -> RunResult:
    """Execute one mirror pass."""
    storage = storage if storage is not None else GitHubStorage.from_config(cfg)
    feed = load_source(cfg)
    manifest = Manifest.load(storage, cfg)

    pending = [item for item in feed.items if item.guid not in manifest]
    pending.sort(key=lambda i: i.pub_ts)  # oldest first: chronological backfill

    deferred: List[SourceItem] = []
    if not backfill and cfg.max_new and len(pending) > cfg.max_new:
        deferred = pending[cfg.max_new:]
        pending = pending[: cfg.max_new]
        log.warning(
            "%d new episode(s) found; mirroring %d this run (--max-new). "
            "Use --backfill for the initial full mirror.",
            len(pending) + len(deferred), len(pending),
        )

    if dry_run:
        return _dry_run_report(cfg, feed, manifest, pending, deferred)

    mirrored: List[str] = []
    failed: List[str] = []
    for index, item in enumerate(pending):
        try:
            _mirror_one(item, cfg, storage, manifest, keep_downloads=keep_downloads)
            mirrored.append(item.title)
        except MirrorError as exc:
            # Loud, named, and fatal to the exit code -- but we keep going so
            # one bad episode does not block the rest of the backfill.
            log.error("FAILED %s", exc)
            failed.append(item.title)
        if index < len(pending) - 1:
            time.sleep(cfg.delay)  # be polite to Acast

    # Refresh archived item XML for episodes still in the source feed, so
    # description/title edits propagate without a re-download.
    for item in feed.items:
        episode = manifest.get(item.guid)
        if episode is not None and episode.item_xml != item.item_xml:
            episode.item_xml = item.item_xml
            episode.title = item.title
            episode.pub_date = item.pub_date
            episode.pub_ts = item.pub_ts

    # Each successful _mirror_one already saved the manifest; the final save
    # only covers metadata refreshes.
    manifest_uploaded = bool(mirrored)
    if manifest.changed:
        manifest.save(storage, cfg)
        manifest_uploaded = True

    episodes = manifest.ordered()
    xml = build_feed(feed, episodes, cfg)
    storage.put_bytes(cfg.feed_key, xml, "application/rss+xml")
    log.info("Published %s", cfg.feed_public_url)

    return RunResult(
        mirrored=mirrored,
        failed=failed,
        deferred=[i.title for i in deferred],
        feed_items=len(episodes),
        feed_uploaded=True,
        manifest_uploaded=manifest_uploaded,
    )


def _dry_run_report(cfg, feed, manifest, pending, deferred) -> RunResult:
    """Report what would happen. Touches nothing."""
    log.info("--- DRY RUN: nothing will be downloaded, uploaded or written ---")
    log.info("Source feed:      %s (%d items)", cfg.feed_url, len(feed.items))
    log.info("Already mirrored: %d episode(s)", len(manifest))

    if pending:
        log.info("Would download %d episode(s):", len(pending))
        for item in pending:
            log.info("  + %-55s %s", item.title[:55], audio_key(cfg, item.guid))
    else:
        log.info("Would download 0 episodes (nothing new).")

    if deferred:
        log.info("Would defer %d episode(s) to a later run:", len(deferred))
        for item in deferred:
            log.info("  ~ %s", item.title)

    would_contain = list(manifest.ordered())
    projected = len(would_contain) + len(pending)
    log.info("feed.xml would contain %d item(s), newest first:", projected)
    for item in sorted(pending, key=lambda i: i.pub_ts, reverse=True)[:5]:
        log.info("  * %-55s (new, %s)", item.title[:55], item.pub_date)
    for episode in would_contain[:5]:
        log.info("  - %-55s %s bytes", episode.title[:55], f"{episode.length:,}")
    if projected > 10:
        log.info("  ... and %d more", projected - 10)

    orphans = [e for e in would_contain if e.guid not in {i.guid for i in feed.items}]
    if orphans:
        log.info(
            "%d mirrored episode(s) are absent from the source feed and would be "
            "kept from the archive:", len(orphans)
        )
        for episode in orphans:
            log.info("  = %s", episode.title)

    return RunResult(
        mirrored=[], failed=[], deferred=[i.title for i in deferred],
        feed_items=projected, feed_uploaded=False, manifest_uploaded=False,
    )
