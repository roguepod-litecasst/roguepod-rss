"""Test scaffolding: a fake Acast origin, a fake public bucket, and a fake
GitHub Releases API, all over real HTTP.

The pipeline is exercised end to end through actual sockets so the download,
validation, range and Content-Type behaviour under test is the real code path,
not a mock of it.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
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
        # Both public hosts point at the fake bucket unless a test overrides
        # them (PipelineOnGitHubTest points audio at a FakeGitHub instead).
        fields = dict(
            feed_url=f"{self.origin_base}/feed.xml",
            github_repo="roguepod-litecasst/podcast-mirror",
            github_token="ghp_test_token",
            audio_base_url=f"{self.bucket_base}/audio",
            feed_base_url=self.bucket_base,
            state_dir=f"{tmpdir}/state",
            download_dir=f"{tmpdir}/downloads",
            delay=0.0,
            timeout=15.0,
            retries=1,
            max_new=0,
        )
        fields.update(overrides)
        return Config(**fields)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class FakeGitHub:
    """Just enough of the GitHub Releases API for GitHubStorage, over real HTTP.

    Mirrors the behaviours that matter: the release does not exist until
    created, asset listings paginate, duplicate asset names are rejected,
    downloads 302 to a blob served as application/octet-stream with
    content-disposition: attachment (which is what GitHub really does, and
    the open question in the PRD).
    """

    TOKEN = "ghp_test_token"

    def __init__(self, repo="roguepod-litecasst/podcast-mirror"):
        self.repo = repo
        self.release = None       # release dict or None
        self.assets = {}          # asset id -> {"name", "size", "content_type", "body"}
        self.requests = []        # (method, path) for every API call
        self.fail_next_upload = 0  # count of uploads to answer with 502
        self._next_id = 100
        self._lock = threading.Lock()

        gh = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _json(self, code, payload=None):
                body = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _asset_json(self, asset_id):
                a = gh.assets[asset_id]
                return {
                    "id": asset_id,
                    "name": a["name"],
                    "size": a["size"],
                    "content_type": a["content_type"],
                    "state": "uploaded",
                    "browser_download_url": f"{gh.base}/download/{gh.release['tag_name']}/{a['name']}",
                }

            def _release_json(self):
                r = dict(gh.release)
                r["assets"] = [self._asset_json(i) for i in sorted(gh.assets)]
                return r

            def _authed(self):
                return self.headers.get("Authorization") == f"Bearer {gh.TOKEN}"

            def _blob(self, name, head_only):
                match = [a for a in gh.assets.values() if a["name"] == name]
                if not match:
                    return self._json(404, {"message": "Not Found"})
                body = match[0]["body"]
                rng = self.headers.get("Range")
                if rng and rng.startswith("bytes="):
                    start, _, end = rng[len("bytes="):].partition("-")
                    start = int(start or 0)
                    end = min(int(end) if end else len(body) - 1, len(body) - 1)
                    chunk = body[start:end + 1]
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
                    body = chunk
                else:
                    self.send_response(200)
                # Exactly what GitHub serves, regardless of the upload's type.
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", f"attachment; filename={name}")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if not head_only:
                    self.wfile.write(body)

            def _route(self, head_only=False):
                parsed = urllib.parse.urlsplit(self.path)
                path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
                gh.requests.append((self.command, path))
                prefix = f"/repos/{gh.repo}/releases"

                # Public download URL: 302 to the blob, like github.com does.
                if path.startswith("/download/"):
                    tag, _, name = path[len("/download/"):].partition("/")
                    if gh.release is None or tag != gh.release["tag_name"]:
                        return self._json(404, {"message": "Not Found"})
                    self.send_response(302)
                    self.send_header("Location", f"{gh.base}/blob/{name}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
                if path.startswith("/blob/"):
                    return self._blob(path[len("/blob/"):], head_only)

                if not self._authed():
                    return self._json(401, {"message": "Bad credentials"})

                if self.command == "GET" and path.startswith(f"{prefix}/tags/"):
                    tag = urllib.parse.unquote(path[len(f"{prefix}/tags/"):])
                    if gh.release is None or gh.release["tag_name"] != tag:
                        return self._json(404, {"message": "Not Found"})
                    return self._json(200, self._release_json())

                if self.command == "POST" and path == prefix:
                    length = int(self.headers.get("Content-Length", 0))
                    payload = json.loads(self.rfile.read(length))
                    with gh._lock:
                        if gh.release is not None:
                            return self._json(422, {"message": "Validation Failed"})
                        gh.release = {"id": 1, "tag_name": payload["tag_name"],
                                      "name": payload.get("name", "")}
                    return self._json(201, self._release_json())

                if gh.release is not None and self.command == "GET" \
                        and path == f"{prefix}/{gh.release['id']}/assets":
                    per_page = min(int(query.get("per_page", ["30"])[0]), 100)
                    page = int(query.get("page", ["1"])[0])
                    ids = sorted(gh.assets)[(page - 1) * per_page: page * per_page]
                    return self._json(200, [self._asset_json(i) for i in ids])

                if self.command == "DELETE" and path.startswith(f"{prefix}/assets/"):
                    asset_id = int(path.rsplit("/", 1)[1])
                    with gh._lock:
                        if asset_id not in gh.assets:
                            return self._json(404, {"message": "Not Found"})
                        del gh.assets[asset_id]
                    return self._json(204)

                if gh.release is not None and self.command == "POST" \
                        and path == f"/upload{prefix}/{gh.release['id']}/assets":
                    name = query.get("name", [""])[0]
                    length = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(length)
                    with gh._lock:
                        if gh.fail_next_upload:
                            gh.fail_next_upload -= 1
                            return self._json(502, {"message": "Server Error"})
                        if any(a["name"] == name for a in gh.assets.values()):
                            return self._json(422, {"message": "Validation Failed",
                                                    "errors": [{"code": "already_exists"}]})
                        asset_id = gh._next_id
                        gh._next_id += 1
                        gh.assets[asset_id] = {
                            "name": name, "size": len(body), "body": body,
                            "content_type": self.headers.get("Content-Type", ""),
                        }
                    return self._json(201, self._asset_json(asset_id))

                return self._json(404, {"message": "Not Found"})

            def do_GET(self):
                self._route()

            def do_HEAD(self):
                self._route(head_only=True)

            def do_POST(self):
                self._route()

            def do_DELETE(self):
                self._route()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"

    @property
    def download_base(self):
        """Stands in for github.com/<repo>/releases/download."""
        return f"{self.base}/download"

    def storage(self, local_dir, **overrides):
        from podcast_mirror.storage import GitHubStorage

        kwargs = dict(
            release_tag="audio",
            local_dir=local_dir,
            api_base=self.base,
            upload_base=f"{self.base}/upload",
            timeout=15.0,
        )
        kwargs.update(overrides)
        return GitHubStorage(self.repo, self.TOKEN, **kwargs)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
