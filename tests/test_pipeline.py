"""End-to-end tests for the mirror pipeline, run against a fake origin+bucket."""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest

from lxml import etree as ET

from podcast_mirror import audio, verify as verify_mod
from podcast_mirror.config import Config
from podcast_mirror.errors import (ConfigError, FeedBuildError, ManifestError,
                                    SourceFeedError)
from podcast_mirror.manifest import Manifest, audio_key
from podcast_mirror.pipeline import run
from tests.support import (ITUNES, NOT_MP3, PLACEHOLDER, TINY_AUDIO, FakeGitHub, FakeWorld)

EPISODES = [
    {"guid": "aaa111", "title": "Vampire Survivors", "pub_date": "Wed, 02 Sep 2026 09:00:00 GMT",
     "episode": 50, "size": 1_500_000, "declared_length": 44834272},
    {"guid": "bbb222", "title": "Hades II", "pub_date": "Wed, 26 Aug 2026 09:00:00 GMT",
     "episode": 49, "size": 1_600_000, "declared_length": 0},
    {"guid": "ccc333", "title": "Balatro", "pub_date": "Wed, 19 Aug 2026 09:00:00 GMT",
     "episode": 48, "size": 1_700_000},
]


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.world = FakeWorld()
        self.addCleanup(self.world.stop)
        self.tmp = tempfile.mkdtemp()
        self.world.publish_source(EPISODES)
        self.cfg = self.world.config(self.tmp)

    def _run(self, **kwargs):
        return run(self.cfg, storage=self.world.storage, **kwargs)

    def _feed(self):
        raw = self.world.storage.get_bytes(self.cfg.feed_key)
        self.assertIsNotNone(raw, "feed.xml was never uploaded")
        return ET.fromstring(raw)

    # ---- core behaviour ------------------------------------------------

    def test_backfill_mirrors_everything(self):
        result = self._run(backfill=True)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(result.mirrored), 3)
        self.assertEqual(result.feed_items, 3)
        for ep in EPISODES:
            key = audio_key(self.cfg, ep["guid"])
            head = self.world.storage.head(key)
            self.assertIsNotNone(head, f"{ep['title']} missing from bucket")
            self.assertEqual(head["content_type"], "audio/mpeg")
            self.assertEqual(head["size"], ep["size"])

    def test_second_run_is_a_noop(self):
        self._run(backfill=True)
        self.world.storage.writes.clear()
        self.world.audio_hits.clear()

        result = self._run()

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.mirrored, [])
        self.assertEqual(self.world.audio_hits, [], "second run re-downloaded audio")
        self.assertEqual(
            self.world.storage.writes, ["feed.xml"],
            f"second run wrote {self.world.storage.writes}, expected only feed.xml",
        )
        self.assertFalse(result.manifest_uploaded)

    def test_manifest_upload_is_reported_when_episodes_mirrored(self):
        result = self._run(backfill=True)
        self.assertTrue(
            result.manifest_uploaded,
            "mirroring episodes must report the manifest as uploaded",
        )
        self.assertIn(self.cfg.manifest_key, self.world.storage.writes)

    def _download_paths(self):
        return [os.path.join(self.cfg.download_dir, os.path.basename(audio_key(self.cfg, ep["guid"])))
                for ep in EPISODES]

    def test_downloads_are_deleted_after_upload_by_default(self):
        self._run(backfill=True)
        for path in self._download_paths():
            self.assertFalse(os.path.exists(path), f"{path} survived the upload")

    def test_keep_downloads_leaves_verified_mp3s_on_disk(self):
        result = self._run(backfill=True, keep_downloads=True)
        self.assertEqual(result.exit_code, 0)
        manifest = Manifest.load(self.world.storage, self.cfg)
        for ep, path in zip(EPISODES, self._download_paths()):
            self.assertTrue(os.path.exists(path), f"{path} was deleted despite --keep-downloads")
            with open(path, "rb") as fh:
                data = fh.read()
            self.assertEqual(len(data), ep["size"])
            self.assertEqual(hashlib.sha256(data).hexdigest(), manifest.get(ep["guid"]).sha256)

    def test_length_is_measured_not_trusted(self):
        """The feed's @length is wrong (or 0); we publish the real byte count."""
        self._run(backfill=True)
        manifest = Manifest.load(self.world.storage, self.cfg)
        for ep in EPISODES:
            self.assertEqual(manifest.get(ep["guid"]).length, ep["size"])

        for item in self._feed().find("channel").findall("item"):
            guid = item.findtext("guid")
            declared = int(item.find("enclosure").get("length"))
            expected = next(e["size"] for e in EPISODES if e["guid"] == guid)
            self.assertEqual(declared, expected)
            self.assertNotEqual(declared, 999)

    def test_feed_enclosure_length_matches_bucket_exactly(self):
        self._run(backfill=True)
        for item in self._feed().find("channel").findall("item"):
            enclosure = item.find("enclosure")
            key = self.cfg.key_for_url(enclosure.get("url"))
            self.assertIsNotNone(key, f"enclosure {enclosure.get('url')} is not on the mirror")
            head = self.world.storage.head(key)
            self.assertIsNotNone(head, f"no stored object for {key}")
            self.assertEqual(int(enclosure.get("length")), head["size"])
            self.assertEqual(enclosure.get("type"), "audio/mpeg")

    # ---- feed fidelity -------------------------------------------------

    def test_channel_metadata_preserved_verbatim(self):
        self._run(backfill=True)
        channel = self._feed().find("channel")
        self.assertEqual(channel.findtext("title"), "RoguePod LiteCast")
        self.assertEqual(channel.findtext("language"), "en")
        self.assertEqual(channel.findtext("description"), "<p>A podcast about roguelikes</p>")
        self.assertEqual(channel.findtext(f"{{{ITUNES}}}explicit"), "true")
        self.assertEqual(
            channel.findtext(f"{{{ITUNES}}}owner/{{{ITUNES}}}email"),
            "roguepodlitecast@gmail.com",
        )
        self.assertEqual(channel.find(f"{{{ITUNES}}}category").get("text"), "Leisure")
        self.assertEqual(channel.find(f"{{{ITUNES}}}image").get("href"), "https://example.test/art.jpg")
        self.assertEqual(channel.findtext("image/url"), "https://example.test/art.jpg")

    def test_item_metadata_preserved(self):
        self._run(backfill=True)
        item = self._feed().find("channel").find("item")
        self.assertEqual(item.findtext("title"), "Vampire Survivors")
        self.assertEqual(item.findtext("pubDate"), "Wed, 02 Sep 2026 09:00:00 GMT")
        self.assertEqual(item.findtext(f"{{{ITUNES}}}duration"), "1:02:03")
        self.assertEqual(item.findtext(f"{{{ITUNES}}}episode"), "50")
        self.assertEqual(item.findtext(f"{{{ITUNES}}}season"), "1")
        self.assertEqual(item.find(f"{{{ITUNES}}}image").get("href"), "https://example.test/ep.jpg")
        self.assertEqual(item.find("guid").get("isPermaLink"), "false")

    def test_items_are_newest_first(self):
        self._run(backfill=True)
        titles = [i.findtext("title") for i in self._feed().find("channel").findall("item")]
        self.assertEqual(titles, ["Vampire Survivors", "Hades II", "Balatro"])

    def test_self_link_points_at_the_mirror(self):
        self._run(backfill=True)
        link = self._feed().find("channel").find("{http://www.w3.org/2005/Atom}link")
        self.assertEqual(link.get("href"), self.cfg.feed_public_url)

    def test_rss_is_valid_2_0(self):
        self._run(backfill=True)
        root = self._feed()
        self.assertEqual(ET.QName(root).localname, "rss")
        self.assertEqual(root.get("version"), "2.0")

    # ---- durability ----------------------------------------------------

    def test_episode_dropped_from_source_stays_in_feed(self):
        self._run(backfill=True)
        # Acast drops the newest episode from the feed.
        self.world.publish_source(EPISODES[1:])
        result = self._run()
        self.assertEqual(result.exit_code, 0)
        titles = [i.findtext("title") for i in self._feed().find("channel").findall("item")]
        self.assertIn("Vampire Survivors", titles)
        self.assertEqual(len(titles), 3)

    def test_source_edits_propagate_without_redownload(self):
        self._run(backfill=True)
        self.world.audio_hits.clear()
        edited = [dict(e) for e in EPISODES]
        edited[0]["description"] = "<p>Corrected show notes</p>"
        self.world.publish_source(edited)

        result = self._run()
        self.assertEqual(self.world.audio_hits, [])
        self.assertTrue(result.manifest_uploaded)
        item = self._feed().find("channel").find("item")
        self.assertEqual(item.findtext("description"), "<p>Corrected show notes</p>")

    # ---- failure handling ----------------------------------------------

    def test_placeholder_response_is_a_hard_failure(self):
        """The exact sphinx failure mode: 200 text/plain, tiny body."""
        self.world.modes["bbb222"] = PLACEHOLDER
        result = self._run(backfill=True)
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.failed, ["Hades II"])
        titles = [i.findtext("title") for i in self._feed().find("channel").findall("item")]
        self.assertNotIn("Hades II", titles, "failed episode must be omitted, not broken")
        self.assertEqual(titles, ["Vampire Survivors", "Balatro"])

    def test_too_small_body_rejected(self):
        self.world.modes["ccc333"] = TINY_AUDIO
        result = self._run(backfill=True)
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.failed, ["Balatro"])
        self.assertIsNone(self.world.storage.head(audio_key(self.cfg, "ccc333")))

    def test_non_mp3_body_rejected(self):
        self.world.modes["aaa111"] = NOT_MP3
        result = self._run(backfill=True)
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.failed, ["Vampire Survivors"])

    def test_failed_episode_retried_next_run(self):
        self.world.modes["bbb222"] = PLACEHOLDER
        self.assertEqual(self._run(backfill=True).exit_code, 1)
        self.world.modes.pop("bbb222")
        result = self._run(backfill=True)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.mirrored, ["Hades II"])
        self.assertEqual(result.feed_items, 3)

    # ---- flags ---------------------------------------------------------

    def test_dry_run_touches_nothing(self):
        result = self._run(dry_run=True, backfill=True)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.feed_items, 3)
        self.assertEqual(self.world.storage.objects, {})
        self.assertEqual(self.world.audio_hits, [])
        self.assertFalse(result.feed_uploaded)

    def test_max_new_defers_the_rest(self):
        self.cfg.max_new = 2
        result = self._run()
        self.assertEqual(len(result.mirrored), 2)
        self.assertEqual(len(result.deferred), 1)
        self.assertEqual(result.exit_code, 0)
        # Oldest first, so the backlog drains chronologically.
        self.assertEqual(result.mirrored, ["Balatro", "Hades II"])

    def test_keys_are_stable_and_derived_from_guid(self):
        key = audio_key(self.cfg, "aaa111")
        self.assertEqual(key, audio_key(self.cfg, "aaa111"))
        self.assertTrue(key.startswith("audio/aaa111-"))
        self.assertTrue(key.endswith(".mp3"))
        weird = audio_key(self.cfg, "https://acast.com/ep?id=1 2/3")
        self.assertNotIn("/", weird[len("audio/"):])

    # ---- unit-level guards ---------------------------------------------

    def test_mp3_sniffing(self):
        self.assertTrue(audio.looks_like_mp3(b"ID3\x03"))
        self.assertTrue(audio.looks_like_mp3(b"\xff\xfb\x90\x00"))
        self.assertFalse(audio.looks_like_mp3(b"ok"))
        self.assertFalse(audio.looks_like_mp3(b"<htm"))
        # 0xFF 0xFF is a legitimate MPEG1 Layer I header, so it must pass.
        self.assertTrue(audio.looks_like_mp3(b"\xff\xff\x00\x00"))
        self.assertFalse(audio.looks_like_mp3(b"\xff\xef\x00\x00"))  # reserved version
        self.assertFalse(audio.looks_like_mp3(b"\xff\xf9\x00\x00"))  # reserved layer


class ErrorPathTest(unittest.TestCase):
    """Failure modes must be loud and specific, never a raw traceback."""

    def setUp(self):
        self.world = FakeWorld()
        self.addCleanup(self.world.stop)
        self.tmp = tempfile.mkdtemp()
        self.world.publish_source(EPISODES)
        self.cfg = self.world.config(self.tmp)

    def test_corrupt_manifest_reports_clearly(self):
        run(self.cfg, storage=self.world.storage, backfill=True)
        self.world.storage.put_bytes(self.cfg.manifest_key, b"{not json", "application/json")
        with self.assertRaises(ManifestError) as ctx:
            run(self.cfg, storage=self.world.storage)
        self.assertIn("unreadable", str(ctx.exception))
        self.assertIn("--backfill", str(ctx.exception))

    def test_missing_archived_xml_is_loud(self):
        run(self.cfg, storage=self.world.storage, backfill=True)
        manifest = Manifest.load(self.world.storage, self.cfg)
        manifest.get("aaa111").item_xml = ""
        manifest.save(self.world.storage, self.cfg)
        # Acast drops that episode, so the archived copy is the only source.
        self.world.publish_source(EPISODES[1:])
        with self.assertRaises(FeedBuildError) as ctx:
            run(self.cfg, storage=self.world.storage)
        self.assertIn("Vampire Survivors", str(ctx.exception))

    def test_unreachable_source_feed_is_a_config_grade_error(self):
        self.cfg.feed_url = "http://127.0.0.1:1/nope.xml"
        with self.assertRaises(SourceFeedError):
            run(self.cfg, storage=self.world.storage)


class PublicUrlConfigTest(unittest.TestCase):
    """Audio and feed live on different hosts; config.py owns that split."""

    REPO = "RoguePod-LiteCasst/podcast-mirror"

    def test_urls_derive_from_the_repository(self):
        cfg = Config(feed_url="f", github_repo=self.REPO)
        self.assertEqual(
            cfg.audio_base_url,
            "https://github.com/RoguePod-LiteCasst/podcast-mirror/releases/download/audio",
        )
        # Pages hostnames are lowercase, whatever case the owner was typed in.
        self.assertEqual(cfg.feed_base_url, "https://roguepod-litecasst.github.io/podcast-mirror")

    def test_audio_keys_route_to_release_assets_by_basename(self):
        cfg = Config(feed_url="f", github_repo=self.REPO)
        self.assertEqual(
            cfg.public_url("audio/ep-abc123.mp3"),
            "https://github.com/RoguePod-LiteCasst/podcast-mirror/releases/download/audio/ep-abc123.mp3",
        )

    def test_other_keys_route_to_pages_at_their_path(self):
        cfg = Config(feed_url="f", github_repo=self.REPO)
        self.assertEqual(cfg.feed_public_url,
                         "https://roguepod-litecasst.github.io/podcast-mirror/feed.xml")
        self.assertEqual(cfg.public_url("state/manifest.json"),
                         "https://roguepod-litecasst.github.io/podcast-mirror/state/manifest.json")

    def test_release_tag_is_part_of_the_audio_url(self):
        cfg = Config(feed_url="f", github_repo=self.REPO, release_tag="v2")
        self.assertTrue(cfg.public_url("audio/x.mp3").endswith("/releases/download/v2/x.mp3"))

    def test_explicit_base_urls_win_and_lose_trailing_slashes(self):
        cfg = Config(feed_url="f", github_repo=self.REPO,
                     audio_base_url="https://pub-1.r2.dev/audio/",
                     feed_base_url="https://mirror.example/")
        self.assertEqual(cfg.public_url("audio/x.mp3"), "https://pub-1.r2.dev/audio/x.mp3")
        self.assertEqual(cfg.feed_public_url, "https://mirror.example/feed.xml")

    def test_key_for_url_inverts_public_url_for_audio_only(self):
        cfg = Config(feed_url="f", github_repo=self.REPO)
        key = "audio/ep-abc123.mp3"
        self.assertEqual(cfg.key_for_url(cfg.public_url(key)), key)
        self.assertIsNone(cfg.key_for_url("https://sphinx.acast.com/x/media.mp3"))
        self.assertIsNone(cfg.key_for_url(cfg.feed_public_url))
        self.assertIsNone(cfg.key_for_url(cfg.audio_base_url + "/"))

    def test_no_repo_and_no_base_urls_is_a_clear_error(self):
        cfg = Config(feed_url="f")
        with self.assertRaises(ConfigError) as ctx:
            cfg.public_url("audio/x.mp3")
        self.assertIn("GITHUB_REPOSITORY", str(ctx.exception))

    def test_malformed_repo_is_rejected_up_front(self):
        with self.assertRaises(ConfigError):
            Config(feed_url="f", github_repo="just-a-name")

    def test_manifest_lives_under_state_by_default(self):
        cfg = Config(feed_url="f", github_repo=self.REPO)
        self.assertEqual(cfg.manifest_key, "state/manifest.json")

    def test_token_stays_out_of_repr(self):
        cfg = Config(feed_url="f", github_repo=self.REPO, github_token="ghp_SUPERSECRET")
        self.assertNotIn("SUPERSECRET", repr(cfg))


class EndpointConfigTest(unittest.TestCase):
    """The S3 fallback (Cloudflare R2) must still configure cleanly."""

    def test_explicit_endpoint_wins(self):
        cfg = Config(feed_url="f", bucket="b",
                     endpoint="https://la-s3.storage.bunnycdn.com", account_id="acct")
        self.assertEqual(cfg.endpoint_url, "https://la-s3.storage.bunnycdn.com")

    def test_endpoint_trailing_slash_stripped(self):
        cfg = Config(feed_url="f", bucket="b", endpoint="https://la-s3.storage.bunnycdn.com/")
        self.assertEqual(cfg.endpoint_url, "https://la-s3.storage.bunnycdn.com")

    def test_falls_back_to_r2_from_account_id(self):
        cfg = Config(feed_url="f", bucket="b", account_id="acct123")
        self.assertEqual(cfg.endpoint_url, "https://acct123.r2.cloudflarestorage.com")

    def test_no_endpoint_is_a_clear_config_error(self):
        cfg = Config(feed_url="f", bucket="b")
        with self.assertRaises(ConfigError) as ctx:
            cfg.endpoint_url
        self.assertIn("STORAGE_ENDPOINT_URL", str(ctx.exception))

    def test_missing_credentials_named_precisely(self):
        cfg = Config(feed_url="f", endpoint="https://la-s3.storage.bunnycdn.com")
        with self.assertRaises(ConfigError) as ctx:
            cfg.require_credentials()
        self.assertIn("STORAGE_BUCKET", str(ctx.exception))
        self.assertIn("STORAGE_ACCESS_KEY_ID", str(ctx.exception))

    def test_credentials_stay_out_of_repr(self):
        cfg = Config(feed_url="f", bucket="b",
                     access_key_id="zone-name", secret_access_key="SUPERSECRET")
        self.assertNotIn("SUPERSECRET", repr(cfg))


class VerifyTest(unittest.TestCase):
    """Exercises verify.py against the fake bucket served over real HTTP."""

    def setUp(self):
        self.world = FakeWorld()
        self.addCleanup(self.world.stop)
        self.tmp = tempfile.mkdtemp()
        self.world.publish_source(EPISODES)
        self.cfg = self.world.config(self.tmp)
        run(self.cfg, storage=self.world.storage, backfill=True)

    def test_verification_passes_on_a_healthy_mirror(self):
        checks = verify_mod.verify(self.cfg, storage=self.world.storage, sample=3)
        self.assertEqual(checks.failures, [])
        self.assertGreaterEqual(checks.passed, 18)
        self.assertEqual(checks.audio_content_types, ["audio/mpeg"] * 3)

    def test_verification_catches_a_length_mismatch(self):
        key = audio_key(self.cfg, "aaa111")
        body, ctype = self.world.storage.objects[key]
        self.world.storage.objects[key] = (body[:-100], ctype)  # corrupt the object
        with self.assertRaises(verify_mod.VerificationError) as ctx:
            verify_mod.verify(self.cfg, storage=self.world.storage, sample=3)
        self.assertIn("length", str(ctx.exception).lower())

    def test_verification_rejects_a_placeholder_content_type(self):
        # Relaxing to octet-stream must not let Acast's text/plain stub through.
        key = audio_key(self.cfg, "aaa111")
        body, _ = self.world.storage.objects[key]
        self.world.storage.objects[key] = (body, "text/plain")
        with self.assertRaises(verify_mod.VerificationError) as ctx:
            verify_mod.verify(self.cfg, storage=self.world.storage, sample=3)
        self.assertIn("Content-Type", str(ctx.exception))
        self.assertIn("text/plain", str(ctx.exception))

    def test_verification_rejects_an_html_feed(self):
        # Accepting application/xml (Pages) must not let an error page through.
        body, _ = self.world.storage.objects["feed.xml"]
        self.world.storage.objects["feed.xml"] = (body, "text/html")
        with self.assertRaises(verify_mod.VerificationError) as ctx:
            verify_mod.verify(self.cfg, storage=self.world.storage, sample=3)
        self.assertIn("feed.xml Content-Type", str(ctx.exception))
        self.assertIn("text/html", str(ctx.exception))


class VerifyOnGitHubTest(unittest.TestCase):
    """verify.py against a fake GitHub release, which serves octet-stream."""

    def setUp(self):
        self.world = FakeWorld()
        self.addCleanup(self.world.stop)
        self.gh = FakeGitHub()
        self.addCleanup(self.gh.stop)
        self.tmp = tempfile.mkdtemp()
        self.world.publish_source(EPISODES)
        self.cfg = self.world.config(self.tmp, audio_base_url=f"{self.gh.download_base}/audio")
        self.storage = self.gh.storage(self.tmp)
        run(self.cfg, storage=self.storage, backfill=True)
        # Stand in for Pages: publish the committed feed.xml at feed_base_url,
        # with the application/xml type Pages gives .xml files.
        with open(os.path.join(self.tmp, "feed.xml"), "rb") as fh:
            self.world.storage.objects["feed.xml"] = (fh.read(), "application/xml; charset=utf-8")

    def test_octet_stream_audio_passes_and_is_recorded(self):
        with self.assertLogs(verify_mod.log, level="WARNING") as captured:
            checks = verify_mod.verify(self.cfg, storage=self.storage, sample=3)
        self.assertEqual(checks.failures, [])
        self.assertEqual(checks.audio_content_types, ["application/octet-stream"] * 3)
        self.assertTrue(any("application/octet-stream" in line for line in captured.output))
        # The HEAD must survive the 302 to the blob store as a HEAD, not a GET.
        blob_methods = {m for m, p in self.gh.requests if p.startswith("/blob/")}
        self.assertIn("HEAD", blob_methods)
        self.assertEqual(len([1 for m, p in self.gh.requests if m == "HEAD" and p.startswith("/blob/")]), 3)

    def test_length_mismatch_still_fails_on_github(self):
        asset = next(iter(self.gh.assets.values()))
        asset["body"] = asset["body"][:-100]
        asset["size"] = len(asset["body"])
        with self.assertRaises(verify_mod.VerificationError) as ctx:
            verify_mod.verify(self.cfg, storage=self.storage, sample=3)
        self.assertIn("length", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
