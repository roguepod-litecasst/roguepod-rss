"""Building the mirrored RSS feed.

Strategy: deep-copy the source <rss> element, drop its <item>s, and re-append
one item per mirrored episode with only the <enclosure> rewritten. Copying the
real tree (rather than re-emitting fields we know about) is what makes
"preserve channel metadata verbatim" true by construction -- including
<itunes:owner><itunes:email>, categories, artwork and any namespaced element we
never thought to enumerate.

Items come from the manifest's archived XML when the source feed no longer
carries them, so an episode Acast drops stays in the output and YouTube never
deletes the corresponding video.
"""

from __future__ import annotations

import copy
import logging
from typing import Dict, List

from lxml import etree as ET

from .errors import FeedBuildError
from .manifest import Episode
from .source import ITUNES, SourceFeed

log = logging.getLogger(__name__)

ATOM = "http://www.w3.org/2005/Atom"


def _rewrite_enclosure(item: ET._Element, url: str, length: int) -> None:
    """Point the enclosure at our mirror, with the true byte length."""
    enclosure = item.find("enclosure")
    if enclosure is None:
        enclosure = ET.SubElement(item, "enclosure")
    enclosure.set("url", url)
    enclosure.set("length", str(length))
    enclosure.set("type", "audio/mpeg")


def _item_for(episode: Episode, source_items: Dict[str, ET._Element]) -> ET._Element:
    """The <item> element for a mirrored episode, source-fresh when possible."""
    element = source_items.get(episode.guid)
    if element is not None:
        # Still in the source feed: copy it so metadata edits propagate.
        return copy.deepcopy(element)
    # Dropped from the source feed: fall back to the archived copy.
    log.info(
        "Episode %r is no longer in the source feed; emitting the archived item.",
        episode.title,
    )
    if not episode.item_xml.strip():
        raise FeedBuildError(
            f"Episode {episode.title!r} (guid={episode.guid}) is absent from the "
            "source feed and has no archived item XML in the manifest, so it "
            "cannot be emitted without losing its metadata."
        )
    try:
        return ET.fromstring(episode.item_xml.encode("utf-8"))
    except ET.XMLSyntaxError as exc:
        raise FeedBuildError(
            f"Archived item XML for {episode.title!r} is malformed: {exc}"
        ) from exc


def build_feed(feed: SourceFeed, episodes: List[Episode], cfg) -> bytes:
    """Render the mirrored feed. ``episodes`` must already be newest-first."""
    root = copy.deepcopy(feed.root)
    channel = root.find("channel")
    for stale in channel.findall("item"):
        channel.remove(stale)

    # A self-link pointing back at Acast would be wrong in a feed served from
    # our bucket; point it at the mirror instead.
    for link in channel.findall(f"{{{ATOM}}}link"):
        if link.get("rel") == "self":
            link.set("href", cfg.feed_public_url)

    # <itunes:new-feed-url> would redirect a consumer straight back to Acast,
    # defeating the mirror. Drop it if the source ever grows one.
    for redirect in channel.findall(f"{{{ITUNES}}}new-feed-url"):
        log.warning("Dropping <itunes:new-feed-url> so YouTube stays on the mirror.")
        channel.remove(redirect)

    source_items = {item.guid: item.element for item in feed.items}
    for episode in episodes:
        item = _item_for(episode, source_items)
        _rewrite_enclosure(item, cfg.public_url(episode.key), episode.length)
        channel.append(item)

    if channel.find(f"{{{ITUNES}}}owner/{{{ITUNES}}}email") is None:
        log.warning(
            "Source channel has no <itunes:owner><itunes:email>; "
            "YouTube ownership verification will fail."
        )

    xml = ET.tostring(
        root.getroottree(), xml_declaration=True, encoding="UTF-8", pretty_print=True
    )
    log.info("Built feed.xml with %d item(s), %s bytes.", len(episodes), f"{len(xml):,}")
    return xml
