"""Test scaffolding: a fake Acast origin and a fake public bucket, over real HTTP.

The pipeline is exercised end to end through actual sockets so the download,
validation, range and Content-Type behaviour under test is the real code path,
not a mock of it.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from lxml import etree as ET

from podcast_mirror.config import Config
from podcast_mirror.storage import MemoryStorage

ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM = "http://www.w3.org/2005/Atom"

# Failure modes an origin can be told to serve for a given guid.
OK = "ok"
PLACEHOLDER = "placeholder"       # the sphinx 2-byte text/plain response
TINY_AUDIO = "tiny"               # audio/* but far too small
NOT_MP3 = "not_mp3"               # right size, wrong magic bytes
SERVER_ERROR = "error"


def make_mp3(size: int = 1_500_000, seed: bytes = b"\x00") -> bytes:
    """A byte string that passes the MP3 sniff: ID3 header plus filler."""
    header = b"ID3\x03\x00\x00\x00\x00\x00\x00"
    return header + (seed * ((size - len(header)) // len(seed) + 1))[: size - len(header)]


def build_source_feed(episodes, origin_base: str, *, owner_email="roguepodlitecast@gmail.com") -> bytes:
    """Build a source feed shaped like the real Acast one."""
    nsmap = {"itunes": ITUNES, "atom": ATOM}
    root = ET.Element("rss", nsmap=nsmap)
    root.set("version", "2.0")
    channel = ET.SubElement(root, "channel")
    ET.SubElement(channel, "title").text = "RoguePod LiteCast"
    ET.SubElement(channel, "link").text = "https://roguepod.show"
    ET.SubElement(channel, "description").text = "<p>A podcast about roguelikes</p>"
    ET.SubElement(channel, "language").text = "en"
    ET.SubElement(channel, "copyright").text = "RoguePod LiteCast"
    self_link = ET.SubElement(channel, f"{{{ATOM}}}link")
    self_link.set("href", f"{origin_base}/feed.xml")
    self_link.set("rel", "self")
    self_link.set("type", "application/rss+xml")
    ET.SubElement(channel, f"{{{ITUNES}}}author").text = "Danny & David"
    ET.SubElement(channel, f"{{{ITUNES}}}explicit").text = "true"
    ET.SubElement(channel, f"{{{ITUNES}}}type").text = "episodic"
    owner = ET.SubElement(channel, f"{{{ITUNES}}}owner")
    ET.SubElement(owner, f"{{{ITUNES}}}name").text = "Danny & David"
    ET.SubElement(owner, f"{{{ITUNES}}}email").text = owner_email
    ET.SubElement(channel, f"{{{ITUNES}}}image").set("href", "https://example.test/art.jpg")
    ET.SubElement(channel, f"{{{ITUNES}}}category").set("text", "Leisure")
    image = ET.SubElement(channel, "image")
    ET.SubElement(image, "url").text = "https://example.test/art.jpg"
    ET.SubElement(image, "title").text = "RoguePod LiteCast"

    for ep in episodes:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = ep["title"]
        ET.SubElement(item, "pubDate").text = ep["pub_date"]
        ET.SubElement(item, f"{{{ITUNES}}}duration").text = ep.get("duration", "1:02:03")
        enclosure = ET.SubElement(item, "enclosure")
        enclosure.set("url", f"{origin_base}/audio/{ep['guid']}.mp3")
        # Deliberately wrong: the real feed's @length is an ad-stitched estimate.
        enclosure.set("length", str(ep.get("declared_length", 999)))
        enclosure.set("type", "audio/mpeg")
        guid = ET.SubElement(item, "guid")
        guid.set("isPermaLink", "false")
        guid.text = ep["guid"]
        ET.SubElement(item, "description").text = ep.get("description", "<p>Notes</p>")
        ET.SubElement(item, f"{{{ITUNES}}}episode").text = str(ep.get("episode", 1))
        ET.SubElement(item, f"{{{ITUNES}}}season").text = str(ep.get("season", 1))
        ET.SubElement(item, f"{{{ITUNES}}}image").set("href", ep.get("art", "https://example.test/ep.jpg"))
    return ET.tostring(root.getroottree(), xml_declaration=True, encoding="UTF-8")


class FakeWorld:
    """Serves the source feed and audio, and the mirrored bucket, over HTTP."""

    def __init__(self):
        self.storage = MemoryStorage()
        self.feed_bytes = b""
        self.audio = {}        # guid -> bytes
        self.modes = {}        # guid -> failure mode
        self.audio_hits = []   # guids that were actually downloaded

        world = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # silence test output
                pass

            def _send(self, code, body=b"", content_type="application/octet-stream", extra=None):
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _bucket(self, key, head_only):
                entry = world.storage.objects.get(key)
                if entry is None:
                    return self._send(404, b"not found", "text/plain")
                body, content_type = entry
                rng = self.headers.get("Range")
                if rng and rng.startswith("bytes="):
                    start, _, end = rng[len("bytes="):].partition("-")
                    start = int(start or 0)
                    end = int(end) if end else len(body) - 1
                    end = min(end, len(body) - 1)
                    chunk = body[start:end + 1]
                    self.send_response(206)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(chunk)))
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
                    self.send_header("Accept-Ranges", "bytes")
                    self.end_headers()
                    if not head_only:
                        self.wfile.write(chunk)
                    return None
                return self._send(200, body, content_type, {"Accept-Ranges": "bytes"})

            def _route(self, head_only=False):
                path = self.path.split("?", 1)[0]
                if path.startswith("/bucket/"):
                    return self._bucket(path[len("/bucket/"):], head_only)
                if path == "/origin/feed.xml":
                    return self._send(200, world.feed_bytes, "application/rss+xml")
                if path.startswith("/origin/audio/"):
                    guid = path[len("/origin/audio/"):].removesuffix(".mp3")
                    mode = world.modes.get(guid, OK)
                    if not head_only:
                        world.audio_hits.append(guid)
                    if mode == PLACEHOLDER:
                        # Exactly what sphinx.acast.com serves on a cold hit.
                        return self._send(200, b"ok", "text/plain; charset=utf-8")
                    if mode == SERVER_ERROR:
                        return self._send(500, b"boom", "text/plain")
                    if mode == TINY_AUDIO:
                        return self._send(200, make_mp3(2048), "audio/mpeg")
                    if mode == NOT_MP3:
                        return self._send(200, b"\x00\x01\x02\x03" + b"x" * 1_500_000, "audio/mpeg")
                    return self._send(200, world.audio[guid], "audio/mpeg")
                return self._send(404, b"nope", "text/plain")

            def do_GET(self):
                self._route()

            def do_HEAD(self):
                self._route(head_only=True)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def origin_base(self):
        return f"http://127.0.0.1:{self.port}/origin"

    @property
    def bucket_base(self):
        return f"http://127.0.0.1:{self.port}/bucket"

    def publish_source(self, episodes):
        for ep in episodes:
            self.audio.setdefault(ep["guid"], make_mp3(ep.get("size", 1_500_000), ep["guid"][:1].encode()))
        self.feed_bytes = build_source_feed(episodes, self.origin_base)

    def config(self, tmpdir, **overrides):
        cfg = Config(
            feed_url=f"{self.origin_base}/feed.xml",
            bucket="test-bucket",
            base_url=self.bucket_base,
            state_dir=f"{tmpdir}/state",
            download_dir=f"{tmpdir}/downloads",
            delay=0.0,
            timeout=15.0,
            retries=1,
            max_new=0,
        )
        for key, value in overrides.items():
            setattr(cfg, key, value)
        return cfg

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
