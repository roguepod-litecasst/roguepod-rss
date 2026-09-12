"""Command-line entrypoint."""

from __future__ import annotations

import argparse
import logging
import sys

from .config import build_config
from .errors import MirrorError
from .pipeline import run
from .verify import verify

COMMANDS = ("mirror", "verify")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mirror.py",
        description="Mirror a podcast feed's audio to GitHub Releases and publish "
                    "a static RSS feed with true byte lengths on GitHub Pages, "
                    "for YouTube ingestion.",
    )
    sub = parser.add_subparsers(dest="command")

    def common(p):
        p.add_argument("--feed-url", help="Source RSS feed (default: $PODCAST_FEED_URL)")
        p.add_argument("--repo", help="GitHub owner/name (default: $GITHUB_REPOSITORY)")
        p.add_argument("--release-tag",
                       help="Release that holds the audio assets (default: $RELEASE_TAG or 'audio')")
        p.add_argument("--audio-base-url",
                       help="Override the public audio URL prefix "
                            "(default: $AUDIO_PUBLIC_BASE_URL, else derived from --repo)")
        p.add_argument("--feed-base-url",
                       help="Override the public feed URL prefix "
                            "(default: $FEED_PUBLIC_BASE_URL, else the repo's Pages URL)")
        p.add_argument("--state-dir", help="Local manifest cache directory")
        p.add_argument("-v", "--verbose", action="store_true", help="Debug logging")

    mirror_p = sub.add_parser("mirror", help="Mirror new episodes and republish feed.xml")
    common(mirror_p)
    mirror_p.add_argument("--download-dir", help="Scratch directory for downloads")
    mirror_p.add_argument("--dry-run", action="store_true",
                          help="Report what would happen; touch nothing")
    mirror_p.add_argument("--backfill", action="store_true",
                          help="Mirror every episode (initial run; do this locally)")
    mirror_p.add_argument("--keep-downloads", action="store_true",
                          help="Leave downloaded MP3s in --download-dir after upload "
                               "(use with --backfill so a storage move never re-downloads)")
    mirror_p.add_argument("--delay", type=float,
                          help="Seconds to wait between downloads (default 2.0)")
    mirror_p.add_argument("--max-new", type=int,
                          help="Cap on episodes mirrored per non-backfill run "
                               "(default 10; 0 means no cap)")
    mirror_p.add_argument("--timeout", type=float, help="Per-request timeout in seconds")

    verify_p = sub.add_parser("verify", help="Assert the published mirror is correct")
    common(verify_p)
    verify_p.add_argument("--sample", type=int, default=3,
                          help="How many episodes to HEAD over HTTP (default 3)")
    return parser


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Make "mirror" the default command so bare `python mirror.py` works.
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help")):
        argv.insert(0, "mirror")
    args = _parser().parse_args(argv)
    if args.command is None:
        args.command = "mirror"

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    # boto3 is chatty at debug level and can echo request metadata.
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("s3transfer").setLevel(logging.WARNING)
    log = logging.getLogger("mirror")

    try:
        cfg = build_config(args)
        if args.command == "verify":
            verify(cfg, sample=args.sample)
            log.info("VERIFIED: the published mirror is correct.")
            return 0

        result = run(cfg, dry_run=args.dry_run, backfill=args.backfill,
                     keep_downloads=args.keep_downloads)
        if args.dry_run:
            log.info("Dry run complete; nothing was changed.")
            return 0

        log.info(
            "Done. mirrored=%d failed=%d deferred=%d feed_items=%d "
            "feed_uploaded=%s manifest_uploaded=%s",
            len(result.mirrored), len(result.failed), len(result.deferred),
            result.feed_items, result.feed_uploaded, result.manifest_uploaded,
        )
        if result.failed:
            log.error("These episodes failed to mirror and were omitted from the feed:")
            for title in result.failed:
                log.error("  - %s", title)
        return result.exit_code
    except MirrorError as exc:
        log.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        log.error("Interrupted. The manifest is consistent; re-run to resume.")
        return 130
