"""GitHubStorage against a fake GitHub Releases API served over real HTTP."""

from __future__ import annotations

import os
import tempfile
import unittest

from lxml import etree as ET

from podcast_mirror.errors import StorageError
from podcast_mirror.manifest import audio_key
from podcast_mirror.pipeline import run
from podcast_mirror.storage import GitHubStorage
from tests.support import FakeGitHub, FakeWorld, make_mp3
from tests.test_pipeline import EPISODES


class GitHubStorageTest(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.addCleanup(self.gh.stop)
        self.tmp = tempfile.mkdtemp()
        self.storage = self.gh.storage(self.tmp)

    def _mp3_file(self, size=4096, seed=b"a"):
        path = os.path.join(self.tmp, f"{seed.decode()}.mp3")
        with open(path, "wb") as fh:
            fh.write(make_mp3(size, seed))
        return path

    # ---- audio/ keys -> release assets -----------------------------------

    def test_audio_upload_creates_release_and_asset(self):
        self.assertIsNone(self.gh.release)
        self.storage.put_file(self._mp3_file(4096), "audio/ep-abc123.mp3", "audio/mpeg")

        self.assertEqual(self.gh.release["tag_name"], "audio")
        names = [a["name"] for a in self.gh.assets.values()]
        self.assertEqual(names, ["ep-abc123.mp3"])  # flat namespace: basename only
        head = self.storage.head("audio/ep-abc123.mp3")
        self.assertEqual(head, {"size": 4096, "content_type": "audio/mpeg"})

    def test_head_is_none_before_release_exists(self):
        self.assertIsNone(self.storage.head("audio/nope.mp3"))
        self.assertIsNone(self.storage.get_bytes("audio/nope.mp3"))
        self.assertIsNone(self.gh.release, "a read must not create the release")

    def test_head_is_none_for_unknown_asset(self):
        self.storage.put_file(self._mp3_file(), "audio/one.mp3", "audio/mpeg")
        self.assertIsNone(self.storage.head("audio/two.mp3"))

    def test_reupload_replaces_the_existing_asset(self):
        self.storage.put_file(self._mp3_file(4096, b"a"), "audio/ep.mp3", "audio/mpeg")
        self.storage.put_file(self._mp3_file(8192, b"b"), "audio/ep.mp3", "audio/mpeg")

        self.assertEqual(len(self.gh.assets), 1, "duplicate names are not allowed")
        self.assertEqual(self.storage.head("audio/ep.mp3")["size"], 8192)
        self.assertIn(("DELETE", f"/repos/{self.gh.repo}/releases/assets/100"), self.gh.requests)

    def test_get_bytes_follows_the_public_download_redirect(self):
        body = make_mp3(4096, b"z")
        self.storage.put_bytes("audio/z.mp3", body, "audio/mpeg")
        self.assertEqual(self.storage.get_bytes("audio/z.mp3"), body)

    def test_asset_listing_paginates(self):
        self.storage._PER_PAGE = 2
        for i in range(5):
            self.storage.put_bytes(f"audio/ep{i}.mp3", make_mp3(2048, str(i).encode()), "audio/mpeg")
        self.assertEqual(len(self.gh.assets), 5)
        self.assertEqual(self.storage.head("audio/ep4.mp3")["size"], 2048)
        self.assertIsNone(self.storage.head("audio/ep5.mp3"))

    def test_upload_retries_a_server_error(self):
        self.storage._RETRY_DELAY = 0
        self.gh.fail_next_upload = 1
        self.storage.put_file(self._mp3_file(), "audio/flaky.mp3", "audio/mpeg")
        self.assertEqual(len(self.gh.assets), 1)
        uploads = [r for r in self.gh.requests if r[0] == "POST" and r[1].startswith("/upload/")]
        self.assertEqual(len(uploads), 2)

    def test_persistent_server_error_is_a_storage_error(self):
        self.storage._RETRY_DELAY = 0
        self.gh.fail_next_upload = 10
        with self.assertRaises(StorageError) as ctx:
            self.storage.put_file(self._mp3_file(), "audio/dead.mp3", "audio/mpeg")
        self.assertEqual(ctx.exception.status, 502)
        self.assertEqual(len(self.gh.assets), 0)

    def test_bad_token_is_a_storage_error_not_a_traceback(self):
        storage = self.gh.storage(self.tmp)
        storage._token = "wrong"
        with self.assertRaises(StorageError) as ctx:
            storage.put_file(self._mp3_file(), "audio/x.mp3", "audio/mpeg")
        self.assertEqual(ctx.exception.status, 401)
        self.assertFalse(ctx.exception.retryable)

    def test_constructor_validates_repo_and_token(self):
        with self.assertRaises(StorageError):
            GitHubStorage("not-a-repo", "token")
        with self.assertRaises(StorageError):
            GitHubStorage("owner/repo", "")

    # ---- everything else -> local files -----------------------------------

    def test_other_keys_are_plain_files_in_the_working_tree(self):
        self.storage.put_bytes("feed.xml", b"<rss/>", "application/rss+xml")
        self.storage.put_bytes("state/manifest.json", b"{}", "application/json")

        with open(os.path.join(self.tmp, "feed.xml"), "rb") as fh:
            self.assertEqual(fh.read(), b"<rss/>")
        with open(os.path.join(self.tmp, "state", "manifest.json"), "rb") as fh:
            self.assertEqual(fh.read(), b"{}")
        self.assertEqual(self.storage.get_bytes("feed.xml"), b"<rss/>")
        self.assertEqual(self.storage.head("feed.xml")["size"], len(b"<rss/>"))
        self.assertEqual(self.gh.requests, [], "local files never touch the API")
        self.assertEqual(os.listdir(self.tmp).count("feed.xml"), 1)
        self.assertFalse([f for f in os.listdir(self.tmp) if f.endswith(".tmp")])

    def test_missing_local_file_is_none(self):
        self.assertIsNone(self.storage.get_bytes("state/manifest.json"))
        self.assertIsNone(self.storage.head("feed.xml"))

    def test_put_file_for_local_key_copies_the_file(self):
        src = os.path.join(self.tmp, "src.xml")
        with open(src, "wb") as fh:
            fh.write(b"<rss/>")
        self.storage.put_file(src, "feed.xml", "application/rss+xml")
        self.assertEqual(self.storage.get_bytes("feed.xml"), b"<rss/>")

    def test_refuses_to_write_outside_the_repo(self):
        with self.assertRaises(StorageError):
            self.storage.put_bytes("../escape.txt", b"x", "text/plain")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "..", "escape.txt")))


class PipelineOnGitHubTest(unittest.TestCase):
    """The unchanged pipeline, run against GitHub-shaped storage."""

    def setUp(self):
        self.world = FakeWorld()
        self.addCleanup(self.world.stop)
        self.gh = FakeGitHub()
        self.addCleanup(self.gh.stop)
        self.tmp = tempfile.mkdtemp()
        self.world.publish_source(EPISODES)
        # Audio comes from the fake release; feed.xml is a file in the tree.
        self.cfg = self.world.config(
            self.tmp,
            audio_base_url=f"{self.gh.download_base}/audio",
        )
        self.storage = self.gh.storage(self.tmp)

    def _uploads(self):
        return [r for r in self.gh.requests if r[0] == "POST" and r[1].startswith("/upload/")]

    def test_backfill_puts_audio_in_the_release_and_files_in_the_tree(self):
        result = run(self.cfg, storage=self.storage, backfill=True)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(result.mirrored), 3)

        expected = {os.path.basename(audio_key(self.cfg, ep["guid"])) for ep in EPISODES}
        self.assertEqual({a["name"] for a in self.gh.assets.values()}, expected)
        for ep in EPISODES:
            head = self.storage.head(audio_key(self.cfg, ep["guid"]))
            self.assertEqual(head["size"], ep["size"])

        feed_path = os.path.join(self.tmp, "feed.xml")
        manifest_path = os.path.join(self.tmp, "state", "manifest.json")
        self.assertTrue(os.path.exists(feed_path))
        self.assertTrue(os.path.exists(manifest_path))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "manifest.json")),
                         "manifest must live at state/manifest.json only")

        root = ET.parse(feed_path).getroot()
        urls = [e.get("url") for e in root.iter("enclosure")]
        self.assertEqual(len(urls), 3)
        for url in urls:
            self.assertTrue(url.startswith(f"{self.gh.download_base}/audio/"), url)
        for enclosure in root.iter("enclosure"):
            name = enclosure.get("url").rsplit("/", 1)[1]
            asset = next(a for a in self.gh.assets.values() if a["name"] == name)
            self.assertEqual(int(enclosure.get("length")), asset["size"])

    def test_second_run_uploads_nothing(self):
        run(self.cfg, storage=self.storage, backfill=True)
        before = len(self._uploads())
        result = run(self.cfg, storage=self.gh.storage(self.tmp))  # fresh client, cold cache
        self.assertEqual(result.mirrored, [])
        self.assertEqual(len(self._uploads()), before)
        self.assertEqual(self.world.audio_hits.count("aaa111"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
