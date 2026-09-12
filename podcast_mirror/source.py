"""Fetching and parsing the source (Acast) feed.

We parse with lxml and keep the original element tree rather than normalising
through feedparser, because the output feed must preserve channel and item
metadata verbatim -- including namespaced elements such as
<itunes:owner><itunes:email>, which YouTube uses for ownership verification.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import List, Optional

from lxml import etree as ET

from .errors import SourceFeedError

log = logging.getLogger(__name__)

ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"


def parse_pubdate(value: str) -> float:
    """RFC-2822 pubDate to a POSIX timestamp; 0.0 when unparseable."""
    if not value:
        return 0.0
    try:
        return parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError, OverflowError):
        log.warning("Unparseable pubDate %r; sorting it oldest.", value)
        return 0.0


@dataclass
class SourceItem:
    """One <item> from the source feed."""

    guid: str
    title: str
    enclosure_url: str
    declared_length: str
    pub_date: str
    pub_ts: float
    element: ET._Element

    @property
    def item_xml(self) -> str:
        return ET.tostring(self.element, encoding="unicode")


@dataclass
class SourceFeed:
    """The parsed source feed."""

    root: ET._Element
    channel: ET._Element
    items: List[SourceItem]

    @property
    def title(self) -> str:
        return (self.channel.findtext("title") or "").strip()

    @property
    def owner_email(self) -> Optional[str]:
        node = self.channel.find(f"{{{ITUNES}}}owner/{{{ITUNES}}}email")
        return node.text.strip() if node is not None and node.text else None


def fetch_feed_bytes(cfg) -> bytes:
    """GET the source feed."""
    req = urllib.request.Request(
        cfg.feed_url,
        headers={"User-Agent": cfg.user_agent, "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
            if resp.status != 200:
                raise SourceFeedError(
                    f"Source feed {cfg.feed_url} returned HTTP {resp.status}"
                )
            return resp.read()
    except urllib.error.URLError as exc:
        raise SourceFeedError(f"Could not fetch source feed {cfg.feed_url}: {exc}") from exc


def parse_feed(raw: bytes) -> SourceFeed:
    """Parse feed bytes into a SourceFeed, validating the RSS shape."""
    try:
        root = ET.fromstring(raw)
    except ET.XMLSyntaxError as exc:
        raise SourceFeedError(f"Source feed is not well-formed XML: {exc}") from exc

    if ET.QName(root).localname != "rss":
        raise SourceFeedError(f"Source feed root is <{root.tag}>, expected <rss>")
    channel = root.find("channel")
    if channel is None:
        raise SourceFeedError("Source feed has no <channel> element")

    items: List[SourceItem] = []
    for element in channel.findall("item"):
        guid = (element.findtext("guid") or "").strip()
        title = (element.findtext("title") or "").strip()
        enclosure = element.find("enclosure")
        if not guid:
            # Without a guid there is no stable key; refuse rather than guess.
            raise SourceFeedError(f"Source item {title!r} has no <guid>")
        if enclosure is None or not enclosure.get("url"):
            log.warning("Item %r has no enclosure URL; skipping it.", title)
            continue
        pub_date = (element.findtext("pubDate") or "").strip()
        items.append(
            SourceItem(
                guid=guid,
                title=title,
                enclosure_url=enclosure.get("url"),
                declared_length=enclosure.get("length", ""),
                pub_date=pub_date,
                pub_ts=parse_pubdate(pub_date),
                element=element,
            )
        )

    counts = Counter(i.guid for i in items)
    duplicates = {guid for guid, n in counts.items() if n > 1}
    if duplicates:
        raise SourceFeedError(f"Source feed has duplicate guid(s): {sorted(duplicates)}")

    if not items:
        raise SourceFeedError("Source feed contains no usable items")

    return SourceFeed(root=root, channel=channel, items=items)


def load_source(cfg) -> SourceFeed:
    feed = parse_feed(fetch_feed_bytes(cfg))
    log.info("Source feed %r: %d item(s).", feed.title, len(feed.items))
    return feed
