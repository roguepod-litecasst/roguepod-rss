"""The mirror manifest: the record of every episode already in R2.

The manifest is the source of truth for what the published feed contains. It
stores each item's original XML so an episode that Acast later drops from the
source feed still appears in our output (YouTube must never see an episode
disappear).

It is written atomically to disk and only after the audio object is safely in
R2, so a crash mid-run can never leave a manifest that references a missing
object.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .errors import ManifestError

log = logging.getLogger(__name__)

MANIFEST_VERSION = 1
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def audio_key(cfg, guid: str) -> str:
    """Derive a stable R2 key from the episode guid.

    Stable across runs and independent of the title, which can be edited. A
    short digest disambiguates guids that slugify to the same string.
    """
    slug = _UNSAFE.sub("-", guid).strip("-.")[:60] or "episode"
    digest = hashlib.sha1(guid.encode("utf-8")).hexdigest()[:12]
    return f"{cfg.audio_prefix.strip('/')}/{slug}-{digest}.mp3"


@dataclass
class Episode:
    """One mirrored episode."""

    guid: str
    title: str
    source_url: str
    key: str
    length: int
    sha256: str
    mirrored_at: str
    pub_date: str = ""
    pub_ts: float = 0.0
    item_xml: str = ""

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "Episode":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Manifest:
    """Collection of mirrored episodes, keyed by guid."""

    episodes: Dict[str, Episode] = field(default_factory=dict)
    updated_at: str = ""
    _baseline: str = ""

    # ---- persistence -------------------------------------------------

    @classmethod
    def load(cls, storage, cfg) -> "Manifest":
        raw = storage.get_bytes(cfg.manifest_key)
        if raw is None:
            log.info("No manifest in R2 at %s; starting a new one.", cfg.manifest_key)
            manifest = cls()
            manifest._baseline = manifest._episodes_blob()
            return manifest

        try:
            data = json.loads(raw.decode("utf-8"))
            episodes = {
                guid: Episode.from_json(item)
                for guid, item in data.get("episodes", {}).items()
            }
        except (ValueError, TypeError) as exc:
            raise ManifestError(
                f"Manifest at r2://{cfg.bucket}/{cfg.manifest_key} is unreadable: {exc}. "
                f"Restore it from {cfg.state_dir}/ or re-run with --backfill."
            ) from exc
        manifest = cls(episodes=episodes, updated_at=data.get("updated_at", ""))
        manifest._baseline = manifest._episodes_blob()
        log.info("Loaded manifest: %d episode(s) already mirrored.", len(episodes))
        return manifest

    def _episodes_blob(self) -> str:
        """Canonical form of the episode data, excluding the timestamp.

        Used to decide whether anything actually changed, so an unchanged run
        does not re-upload the manifest.
        """
        return json.dumps(
            {g: e.to_json() for g, e in sorted(self.episodes.items())},
            sort_keys=True,
            ensure_ascii=False,
        )

    @property
    def changed(self) -> bool:
        return self._episodes_blob() != self._baseline

    def to_bytes(self) -> bytes:
        payload = {
            "version": MANIFEST_VERSION,
            "updated_at": self.updated_at,
            "episodes": {g: e.to_json() for g, e in sorted(self.episodes.items())},
        }
        return json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")

    def save(self, storage, cfg) -> None:
        """Write the manifest atomically to disk, then upload it to R2.

        Called after each successful episode upload so an interrupted backfill
        resumes where it stopped.
        """
        self.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        blob = self.to_bytes()

        os.makedirs(cfg.state_dir, exist_ok=True)
        local = os.path.join(cfg.state_dir, os.path.basename(cfg.manifest_key))
        fd, tmp = tempfile.mkstemp(dir=cfg.state_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(blob)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, local)  # atomic within the filesystem
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

        # A single PUT is atomic at the object level in R2.
        storage.put_bytes(cfg.manifest_key, blob, "application/json")
        self._baseline = self._episodes_blob()

    # ---- queries -----------------------------------------------------

    def __contains__(self, guid: str) -> bool:
        return guid in self.episodes

    def __len__(self) -> int:
        return len(self.episodes)

    def add(self, episode: Episode) -> None:
        self.episodes[episode.guid] = episode

    def get(self, guid: str) -> Optional[Episode]:
        return self.episodes.get(guid)

    def ordered(self) -> List[Episode]:
        """Every mirrored episode, newest first."""
        return sorted(
            self.episodes.values(), key=lambda e: (e.pub_ts, e.guid), reverse=True
        )
