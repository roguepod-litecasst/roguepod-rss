"""Post-publish verification against the live public URLs.

These are assertions, not eyeballing: every check either passes or fails the
process. They target the public URLs a YouTube fetcher would hit, because the
original bug was a fetcher-visible one (a cold HEAD returning a 2-byte
text/plain body) that a local-file check would never have caught.

Audio may come back as ``audio/mpeg`` (R2 fallback) or as
``application/octet-stream`` (GitHub Releases, which ignores the upload's
type). Both pass; which one was seen is logged loudly, because that is the
answer to the PRD's open question.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import List, Optional

from lxml import etree as ET

from .errors import VerificationError
from .manifest import Manifest
from .source import ITUNES
from .storage import GitHubStorage

log = logging.getLogger(__name__)

# What a public audio URL may answer with. GitHub Releases always serves
# octet-stream; R2/S3 echo the uploaded type. Anything else (text/plain,
# text/html) is the placeholder/error page this project exists to avoid.
AUDIO_CONTENT_TYPES = ("audio/mpeg", "application/octet-stream")


@dataclass
class Checks:
    """Collects assertion results so one run reports every failure at once."""

    passed: int = 0
    failures: List[str] = field(default_factory=list)
    # Content-Type seen on each audio HEAD, in sample order. Exposed so the
    # caller (and tests) can see which of AUDIO_CONTENT_TYPES the host used.
    audio_content_types: List[str] = field(default_factory=list)

    def check(self, ok: bool, description: str, detail: str = "") -> bool:
        if ok:
            self.passed += 1
            log.info("  PASS  %s%s", description, f" [{detail}]" if detail else "")
        else:
            self.failures.append(f"{description}{f' [{detail}]' if detail else ''}")
            log.error("  FAIL  %s%s", description, f" [{detail}]" if detail else "")
        return ok


class _KeepMethodRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow 30x without downgrading HEAD to GET.

    GitHub release URLs always 302 to the blob store, and the stdlib handler
    re-issues the redirected request as a GET, which would make the "HEAD
    returns 200" check a full-body download whose body we then discard.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and req.get_method() == "HEAD":
            new.method = "HEAD"
        return new


_opener = urllib.request.build_opener(_KeepMethodRedirectHandler)


def _request(url: str, cfg, method: str = "GET", headers: Optional[dict] = None):
    request = urllib.request.Request(
        url, method=method, headers={"User-Agent": cfg.user_agent, **(headers or {})}
    )
    return _opener.open(request, timeout=cfg.timeout)


def verify(cfg, storage=None, sample: int = 3) -> Checks:
    """Run every verification. Raises VerificationError if any check fails."""
    storage = storage if storage is not None else GitHubStorage.from_config(cfg)
    checks = Checks()
    manifest = Manifest.load(storage, cfg)
    if not len(manifest):
        raise VerificationError("Manifest is empty; nothing to verify. Run a backfill first.")

    episodes = manifest.ordered()
    sampled = episodes[:sample]

    log.info("1. HEAD on %d mirrored episode(s) via the public URL", len(sampled))
    for episode in sampled:
        url = cfg.public_url(episode.key)
        log.info(" %s", episode.title)
        try:
            with _request(url, cfg, method="HEAD") as resp:
                status = resp.status
                content_type = resp.headers.get_content_type()
                length = resp.headers.get("Content-Length")
                ranges = (resp.headers.get("Accept-Ranges") or "").lower()
        except urllib.error.HTTPError as exc:
            checks.check(False, f"HEAD {url}", f"HTTP {exc.code}")
            continue
        except urllib.error.URLError as exc:
            checks.check(False, f"HEAD {url}", str(exc))
            continue

        checks.check(status == 200, "HEAD returns 200", f"got {status}")
        checks.audio_content_types.append(content_type)
        if checks.check(
            content_type in AUDIO_CONTENT_TYPES,
            "Content-Type is audio/mpeg or application/octet-stream",
            f"got {content_type!r}",
        ):
            if content_type == "audio/mpeg":
                log.info("  NOTE  audio served as audio/mpeg")
            else:
                log.warning(
                    "  NOTE  audio served as %s, not audio/mpeg — the host ignores the "
                    "upload type. YouTube must go by the <enclosure type> attribute "
                    "and the MP3 bytes; watch whether Stage 1 produces a video.",
                    content_type,
                )
        checks.check(
            length is not None and int(length) == episode.length,
            "Content-Length matches the mirrored byte length",
            f"header {length}, manifest {episode.length}",
        )
        checks.check(ranges == "bytes", "accept-ranges: bytes", f"got {ranges!r}")

    log.info("2. Range GET (bytes=0-1023)")
    for episode in sampled[:1]:
        url = cfg.public_url(episode.key)
        try:
            with _request(url, cfg, headers={"Range": "bytes=0-1023"}) as resp:
                status = resp.status
                content_range = resp.headers.get("Content-Range", "")
                body = resp.read()
        except urllib.error.HTTPError as exc:
            checks.check(False, f"Range GET {url}", f"HTTP {exc.code}")
            continue
        expected_range = f"bytes 0-1023/{episode.length}"
        checks.check(status == 206, "Range GET returns 206", f"got {status}")
        checks.check(
            content_range == expected_range,
            "Content-Range is correct",
            f"got {content_range!r}, expected {expected_range!r}",
        )
        checks.check(len(body) == 1024, "Range body is 1024 bytes", f"got {len(body)}")

    log.info("3. feed.xml parses as RSS 2.0 and every @length matches the stored object")
    feed_url = cfg.feed_public_url
    try:
        with _request(feed_url, cfg) as resp:
            feed_status = resp.status
            feed_ct = resp.headers.get_content_type()
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise VerificationError(f"Could not fetch {feed_url}: HTTP {exc.code}") from exc

    checks.check(feed_status == 200, "feed.xml returns 200", f"got {feed_status}")
    checks.check(
        feed_ct == "application/rss+xml",
        "feed.xml Content-Type is application/rss+xml",
        f"got {feed_ct!r}",
    )

    try:
        root = ET.fromstring(raw)
    except ET.XMLSyntaxError as exc:
        raise VerificationError(f"feed.xml is not well-formed XML: {exc}") from exc

    checks.check(ET.QName(root).localname == "rss", "root element is <rss>")
    checks.check(root.get("version") == "2.0", "RSS version is 2.0", f"got {root.get('version')!r}")
    channel = root.find("channel")
    checks.check(channel is not None, "feed has a <channel>")
    if channel is None:
        raise VerificationError("feed.xml has no <channel>")

    email = channel.find(f"{{{ITUNES}}}owner/{{{ITUNES}}}email")
    checks.check(
        email is not None and bool((email.text or "").strip()),
        "itunes:owner/itunes:email is present (YouTube ownership verification)",
        (email.text or "").strip() if email is not None else "absent",
    )

    items = channel.findall("item")
    checks.check(
        len(items) == len(episodes),
        "feed item count matches the manifest",
        f"feed {len(items)}, manifest {len(episodes)}",
    )

    log.info("   checking %d enclosure(s) against actual stored object sizes", len(items))
    mismatches = 0
    for item in items:
        title = (item.findtext("title") or "?").strip()
        enclosure = item.find("enclosure")
        if enclosure is None:
            checks.check(False, f"item {title!r} has an <enclosure>")
            mismatches += 1
            continue
        url = enclosure.get("url", "")
        key = cfg.key_for_url(url)
        if key is None:
            checks.check(False, f"item {title!r} enclosure points at the mirror", url)
            mismatches += 1
            continue
        head = storage.head(key)
        declared = enclosure.get("length", "")
        if head is None:
            checks.check(False, f"item {title!r}: stored object exists", key)
            mismatches += 1
        elif not declared.isdigit() or int(declared) != head["size"]:
            checks.check(
                False,
                f"item {title!r}: @length matches stored object size",
                f"@length={declared!r}, stored={head['size']}",
            )
            mismatches += 1
        elif head["content_type"] not in AUDIO_CONTENT_TYPES:
            checks.check(
                False, f"item {title!r}: stored Content-Type", head["content_type"]
            )
            mismatches += 1

    checks.check(
        mismatches == 0,
        f"all {len(items)} enclosure @length values match their stored objects",
        f"{mismatches} mismatch(es)",
    )

    log.info("%d check(s) passed, %d failed.", checks.passed, len(checks.failures))
    if checks.failures:
        raise VerificationError(
            f"{len(checks.failures)} verification check(s) failed:\n  - "
            + "\n  - ".join(checks.failures)
        )
    return checks
