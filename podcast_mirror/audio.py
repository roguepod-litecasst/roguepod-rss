"""Downloading and validating episode audio.

We never issue a HEAD against the source: sphinx.acast.com answers a cold HEAD
with "200 text/plain, content-length: 2", which is the exact behaviour that
made YouTube's fetcher give up. Only a GET returns real audio.

Every download passes three gates before it is allowed near R2:
  1. the final response Content-Type is audio/*
  2. the body is at least min_bytes (a placeholder response is a few bytes)
  3. the file begins with an ID3 tag or a valid MPEG audio frame header
Any failure is a hard error naming the episode, never a silent skip.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .errors import DownloadError

log = logging.getLogger(__name__)

CHUNK = 1024 * 256
_MAGIC_BYTES = 4


@dataclass
class DownloadResult:
    """What actually landed on disk -- the only length we trust."""

    path: str
    length: int
    sha256: str
    content_type: str


def looks_like_mp3(head: bytes) -> bool:
    """True if the bytes start with an ID3 tag or a plausible MPEG frame."""
    if head[:3] == b"ID3":
        return True
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        version = (head[1] >> 3) & 0x03
        layer = (head[1] >> 1) & 0x03
        # version 01 and layer 00 are reserved: a false sync, not real audio.
        return version != 0x01 and layer != 0x00
    return False


def _get(url: str, cfg):
    request = urllib.request.Request(
        url, headers={"User-Agent": cfg.user_agent, "Accept": "*/*"}
    )
    # urlopen follows redirects, so this resolves the sphinx -> stitcher hop
    # and the headers we inspect are the final response's.
    return urllib.request.urlopen(request, timeout=cfg.timeout)


def download_episode(url: str, dest: str, title: str, cfg) -> DownloadResult:
    """Stream an enclosure to ``dest``, validating as we go.

    Network faults are retried; a validation failure is final and raises
    immediately, because a wrong-content-type or too-small body means the
    source is serving us the placeholder, not a transient blip.
    """
    last_error = None
    for attempt in range(1, cfg.retries + 1):
        try:
            return _download_once(url, dest, title, cfg)
        except DownloadError:
            raise  # validation failure: do not retry
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last_error = exc
            if attempt < cfg.retries:
                backoff = 2.0 * attempt
                log.warning(
                    "Download of %r failed (attempt %d/%d): %s; retrying in %.0fs",
                    title, attempt, cfg.retries, exc, backoff,
                )
                time.sleep(backoff)
    raise DownloadError(title, f"download failed after {cfg.retries} attempts: {last_error}")


def _download_once(url: str, dest: str, title: str, cfg) -> DownloadResult:
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    digest = hashlib.sha256()
    written = 0
    first_chunk = b""

    with _get(url, cfg) as resp:
        status = getattr(resp, "status", 200)
        if status != 200:
            raise DownloadError(title, f"enclosure returned HTTP {status}")

        content_type = resp.headers.get_content_type()
        if not content_type.startswith("audio/"):
            raise DownloadError(
                title,
                f"enclosure Content-Type is {content_type!r}, expected audio/* "
                f"(this is the placeholder-response failure mode) at {url}",
            )

        declared = resp.headers.get("Content-Length")
        expected = int(declared) if declared and declared.isdigit() else None

        with open(dest, "wb") as out:
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                if not first_chunk:
                    first_chunk = chunk[:_MAGIC_BYTES]
                out.write(chunk)
                digest.update(chunk)
                written += len(chunk)
            out.flush()
            os.fsync(out.fileno())

    if expected is not None and written != expected:
        raise DownloadError(
            title,
            f"truncated download: got {written} bytes, server declared {expected}",
        )
    if written < cfg.min_bytes:
        raise DownloadError(
            title,
            f"body is only {written} bytes, below the {cfg.min_bytes}-byte floor "
            "(placeholder response, not audio)",
        )
    if not looks_like_mp3(first_chunk):
        raise DownloadError(
            title,
            f"file does not start with an ID3 tag or MPEG frame header "
            f"(first bytes: {first_chunk.hex()})",
        )

    log.info("  downloaded %s bytes (%s)", f"{written:,}", content_type)
    return DownloadResult(
        path=dest, length=written, sha256=digest.hexdigest(), content_type=content_type
    )
