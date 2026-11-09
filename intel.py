"""Full-text intelligence: EastMoney news sections with article bodies.

Index pages give us the article ids and headlines; each new article is then
fetched once and its `#ContentBody` stored as plain text, so the dashboard (and
any agent) can read the piece without leaving the platform or hitting the web.

Sections implemented:
    7x24快讯     newsapi kuaixun feed (has digest text)
    财经导读     finance.eastmoney.com front page
    东方视点     finance.eastmoney.com/news/cdfsd.html
    全球         global.eastmoney.com
    焦点专题     topic.eastmoney.com
    社区         guba.eastmoney.com hot posts
"""

from __future__ import annotations

import html as htmlmod
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from em_client import EastMoneyClient, EastMoneyError, num

log = logging.getLogger("em.intel")

CN_TZ = timezone(timedelta(hours=8))
ARTICLE_ID = re.compile(r"eastmoney\.com/a/(\d{16,20})\.html")
TOPIC_LINK = re.compile(r'href="(?:https?:)?//topic\.eastmoney\.com/([A-Za-z0-9_]{2,40})/?"')
TAG = re.compile(r"<[^>]+>")
BLOCK_TAG = re.compile(r"<\s*(?:br|/p|/div|/li|/h[1-6]|/tr|/section)\s*/?>", re.I)
SPACES = re.compile(r"[ \t\r\f\v]+")
NEWLINES = re.compile(r"\n{3,}")


@dataclass(frozen=True, slots=True)
class Section:
    id: str
    name: str
    kind: str        # "json" | "html" | "topic"
    host: str
    path: str
    note: str = ""


SECTIONS: tuple[Section, ...] = (
    Section("kuaixun", "7x24快讯", "json", "newsapi.eastmoney.com",
            "/kuaixun/v1/getlist_102_ajaxResult_50_1_.html", "全市场分钟级快讯"),
    Section("headline", "财经导读", "html", "finance.eastmoney.com", "/", "财经首页头条"),
    Section("viewpoint", "东方视点", "html", "finance.eastmoney.com",
            "/news/cdfsd.html", "评论与深度"),
    Section("global", "全球", "html", "global.eastmoney.com", "/", "海外市场"),
    Section("topic", "焦点专题", "topic", "www.eastmoney.com", "/", "专题聚合"),
    Section("community", "社区", "community", "guba.eastmoney.com", "/", "股吧热帖"),
)

SECTION_MAP = {s.id: s for s in SECTIONS}


def cn_now() -> datetime:
    return datetime.now(CN_TZ)


def clean_text(raw: str) -> str:
    text = raw.replace("\u3000", " ").replace("&nbsp;", " ")
    # inline tags vanish; block tags become paragraph breaks
    text = BLOCK_TAG.sub("\n", text)
    text = TAG.sub("", text)
    text = htmlmod.unescape(text)
    text = SPACES.sub(" ", text)
    text = re.sub(r"\n[ \t]*", "\n", text)
    text = NEWLINES.sub("\n\n", text)
    return text.strip()


def _published_from_id(art_id: str) -> int:
    try:
        day = datetime.strptime(art_id[:8], "%Y%m%d").replace(tzinfo=CN_TZ)
        return int(day.timestamp())
    except ValueError:
        return int(time.time())


# --------------------------------------------------------------------------
# index parsers
# --------------------------------------------------------------------------
def _parse_article_links(page: str, host: str) -> list[dict]:
    """Pull (art_id, title) pairs out of an index page, keeping anchor text."""
    out: list[dict] = []
    seen: set[str] = set()
    pattern = re.compile(
        r'<a[^>]+href="(?:https?:)?//(?:[a-z0-9]+\.)?eastmoney\.com/a/(\d{16,20})\.html"[^>]*>'
        r"\s*(?:<[^>]+>)*([^<]{4,120})(?:<[^>]+>)*\s*</a>", re.I)
    for m in pattern.finditer(page):
        art_id, title = m.group(1), htmlmod.unescape(m.group(2)).strip()
        title = re.sub(r"\s+", " ", title)
        if not title or len(title) < 6 or art_id in seen:
            continue
        seen.add(art_id)
        out.append({"art_id": art_id, "title": title, "section": "", "url":
                    f"https://{host}/a/{art_id}.html",
                    "published": _published_from_id(art_id), "summary": "",
                    "media": ""})
    return out


def _parse_kuaixun(client: EastMoneyClient) -> list[dict]:
    body = client.get_text(SECTIONS[0].path, {}, host=SECTIONS[0].host)
    payload = body[body.find("{"): body.rfind("}") + 1]
    import json as _json
    data = _json.loads(payload)
    out = []
    for item in data.get("LivesList") or []:
        art_id = str(item.get("newsid") or item.get("id") or "")
        title = (item.get("title") or item.get("simtitle") or "").strip()
        digest = (item.get("digest") or "").strip()
        url = item.get("url_w") or item.get("url_unique") or ""
        show = item.get("showtime") or item.get("showTime") or ""
        try:
            published = int(datetime.strptime(show, "%Y-%m-%d %H:%M:%S")
                            .replace(tzinfo=CN_TZ).timestamp())
        except ValueError:
            published = _published_from_id(art_id)
        if not art_id or not title:
            continue
        out.append({"art_id": art_id, "title": title, "section": "kuaixun", "url": url,
                    "published": published, "summary": clean_text(digest)[:600],
                    "media": "东方财富"})
    return out


def _parse_topics(page: str) -> list[dict]:
    out, seen = [], set()
    pattern = re.compile(
        r'<a[^>]+href="(?:https?:)?//topic\.eastmoney\.com/detail/([A-Za-z0-9_]+)\.html"[^>]*>'
        r"(?:<[^>]+>)*([^<]{4,80})", re.I)
    for m in pattern.finditer(page):
        slug, title = m.group(1), htmlmod.unescape(m.group(2)).strip()
        if slug in seen or len(title) < 4:
            continue
        seen.add(slug)
        out.append({"art_id": f"topic:{slug}", "title": re.sub(r"\s+", " ", title),
                    "section": "topic", "url":
                    f"https://topic.eastmoney.com/detail/{slug}.html",
                    "published": int(time.time()), "summary": "", "media": "专题"})
    return out


GUBA_POST = re.compile(
    r'<div class="title"><a[^>]+href="/news,(\d{6}),(\d+)\.html"[^>]*>([^<]{2,120})</a></div>'
    r'.*?<div class="author">.*?<a[^>]*>([^<]{1,40})</a>.*?<div class="update">([^<]{4,20})</div>',
    re.S)


def _parse_community(page: str, code: str) -> list[dict]:
    out = []
    for m in GUBA_POST.finditer(page):
        code_, post_id, title, author, when = m.groups()
        if code_ != code:
            continue
        try:
            published = int(datetime.strptime(f"{cn_now().year}-{when.strip()}",
                                              "%Y-%m-%d %H:%M").replace(tzinfo=CN_TZ).timestamp())
        except ValueError:
            published = int(time.time())
        out.append({"art_id": f"guba:{post_id}", "title": re.sub(r"\s+", " ", title).strip(),
                    "section": "community", "url":
                    f"https://guba.eastmoney.com/news,{code},{post_id}.html",
                    "published": published, "summary": "", "media": author})
    return out


def collect_index(client: EastMoneyClient, section: Section, db=None) -> list[dict]:
    if section.kind == "json":
        return _parse_kuaixun(client)
    if section.kind == "community":
        # posts for the watchlist, topped up with today's hottest names
        codes: list[str] = []
        if db is not None:
            for item in db.watchlist()[:4]:
                codes.append(item["secid"].split(".")[-1])
            for row in db.signals("popularity", None, 4, "desc"):
                code = (row.get("stock") or "").split(".")[-1]
                if code and code not in codes:
                    codes.append(code)
        out = []
        for code in codes[:6]:
            try:
                page = client.get_text(f"/list,{code}.html", {}, host="guba.eastmoney.com")
            except EastMoneyError as exc:
                log.debug("guba %s failed: %s", code, exc)
                continue
            out.extend(_parse_community(page, code))
        return out
    page = client.get_text(section.path, {}, host=section.host)
    if section.kind == "topic":
        return _parse_topics(page)
    items = _parse_article_links(page, section.host)
    for item in items:
        item["section"] = section.id
    return items


# --------------------------------------------------------------------------
# article body
# --------------------------------------------------------------------------
BODY_PATTERNS = (
    re.compile(r'<div[^>]+id="ContentBody"[^>]*>(.*?)</div>', re.S),
    re.compile(r'<div[^>]+class="[^"]*txtinfos[^"]*"[^>]*>(.*?)</div>', re.S),
    re.compile(r"<article[^>]*>(.*?)</article>", re.S),
)


def _balanced_div(page: str, marker: str) -> str:
    """Outer HTML of the div containing `marker`, matched with a depth counter.

    EastMoney bodies nest divs, so a non-greedy '</div>' match stops at the
    first inner close tag and loses almost all of the text.
    """
    idx = page.find(marker)
    if idx < 0:
        return ""
    start = page.rfind("<div", 0, idx)
    if start < 0:
        return ""
    depth = 0
    for match in re.finditer(r"<div\b|</div\s*>", page[start:]):
        depth += 1 if match.group(0).startswith("<div") else -1
        if depth == 0:
            return page[start:start + match.end()]
    return page[start:]


def fetch_body(client: EastMoneyClient, url: str) -> str:
    host = re.sub(r"^https?://", "", url).split("/")[0]
    path = "/" + url.split(host, 1)[1].lstrip("/") if host in url else "/"
    page = client.get_text(path, {}, host=host)
    for marker in ('id="ContentBody"', 'class="txtinfos"'):
        chunk = _balanced_div(page, marker)
        if chunk:
            text = clean_text(chunk)
            if len(text) > 30:
                return text[:20000]
    for pattern in BODY_PATTERNS:
        match = pattern.search(page)
        if not match:
            continue
        text = clean_text(match.group(1))
        if len(text) > 40:
            return text[:20000]
    # fall back to the largest block of text we can find
    body = re.search(r'<div[^>]+class="[^"]*(?:article|content)[^"]*"[^>]*>(.*?)</div>', page, re.S)
    return clean_text(body.group(1))[:20000] if body else ""


# --------------------------------------------------------------------------
# pipeline
# --------------------------------------------------------------------------
class IntelHub:
    def __init__(self, settings, db, client, clock=None):
        self.cfg = settings
        self.db = db
        self.client = client
        self.clock = clock
        self.stop_event = threading.Event()
        self.stats = {"cycles": 0, "indexed": 0, "bodies": 0, "errors": 0}
        self._lock = threading.Lock()

    def now(self) -> float:
        return self.clock.now() if self.clock else time.time()

    def run_once(self, max_bodies: int | None = None) -> dict:
        max_bodies = max_bodies or self.cfg.intel_bodies_per_cycle
        added = 0
        for section in SECTIONS:
            try:
                items = collect_index(self.client, section, self.db)
            except (EastMoneyError, ValueError, OSError) as exc:
                self.stats["errors"] += 1
                log.warning("intel %s index failed: %s", section.id, exc)
                continue
            fresh = [i for i in items if i["art_id"] not in self.db.article_ids([i["art_id"] for i in items])]
            if fresh:
                added += self.db.upsert_articles(fresh)
            log.info("intel %-10s index=%3d new=%3d", section.id, len(items), len(fresh))
            time.sleep(0.15)

        pending = self.db.pending_articles(limit=max_bodies, since_hours=36)
        filled = 0
        for item in pending:
            if item["section"] in ("topic", "kuaixun"):
                continue            # topic pages and flash news have no article body
            try:
                body = fetch_body(self.client, item["url"])
            except (EastMoneyError, OSError) as exc:
                log.debug("intel body %s failed: %s", item["art_id"], exc)
                continue
            if body:
                self.db.upsert_articles([{"art_id": item["art_id"], "section": item["section"],
                                          "title": item["title"], "url": item["url"],
                                          "body": body,
                                          "published": item.get("published") or 0}])
                filled += 1
            time.sleep(0.2)

        with self._lock:
            self.stats["cycles"] += 1
            self.stats["indexed"] += added
            self.stats["bodies"] += filled
        log.info("intel cycle: %d indexed, %d bodies filled", added, filled)
        return {"indexed": added, "bodies": filled}

    def loop(self) -> None:
        log.info("intel hub started (%d sections)", len(SECTIONS))
        first = True
        while not self.stop_event.is_set():
            try:
                self.run_once(max_bodies=60 if first else self.cfg.intel_bodies_per_cycle)
                first = False
                if self.now() - getattr(self, "_pruned", 0) > 3600:
                    removed = self.db.prune_articles(self.cfg.signal_retention_days)
                    self._pruned = self.now()
                    if removed:
                        log.info("intel retention: dropped %d articles", removed)
            except Exception:
                log.exception("intel hub error")
            self.stop_event.wait(self.cfg.intel_interval)

    def start(self) -> None:
        threading.Thread(target=self.loop, name="intel", daemon=True).start()

    def status(self) -> dict:
        return {"sections": [{"id": s.id, "name": s.name, "note": s.note} for s in SECTIONS],
                "stats": self.stats, "interval": self.cfg.intel_interval,
                "bodies_per_cycle": self.cfg.intel_bodies_per_cycle}
