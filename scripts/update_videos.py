#!/usr/bin/env python3
"""I-55 Corridor daily updater.

Runs every day from GitHub Actions. It:
  1. Finds each outlet's social links (YouTube, Facebook, Instagram, X, TikTok)
     by reading the outlet's website, unless you already filled them in
     data/outlets.json. Results go to data/discovered.json.
  2. Looks up the YouTube channel for every outlet and every featured person.
  3. Pulls each featured person's newest videos:
       - everything from the person's own YouTube channel, and
       - videos on their station's channel whose title or description
         mentions them by name (the names listed in "match").
  4. Writes the result to data/videos.json, which the map reads.

Uses only the Python standard library, so there is nothing to install.
"""
import json
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
VIDEOS_PER_PERSON = 12
UA = "Mozilla/5.0 (compatible; I55CorridorBot/1.0; +https://github.com/)"

SOCIAL_PATTERNS = {
    "youtube": r"https?://(?:www\.)?youtube\.com/(?:@[\w.\-]+|channel/UC[\w\-]{22}|c/[\w.\-]+|user/[\w.\-]+)",
    "facebook": r"https?://(?:www\.)?facebook\.com/(?!sharer|share|dialog|plugins|tr\b)[\w.\-]+/?",
    "instagram": r"https?://(?:www\.)?instagram\.com/(?!p/|reel/|explore/)[\w.\-]+/?",
    "x": r"https?://(?:www\.)?(?:twitter|x)\.com/(?!intent|share|home)[\w]+/?",
    "tiktok": r"https?://(?:www\.)?tiktok\.com/@[\w.\-]+/?",
}
ATOM = {"a": "http://www.w3.org/2005/Atom",
        "yt": "http://www.youtube.com/xml/schemas/2015",
        "media": "http://search.yahoo.com/mrss/"}


def get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def load(name, default):
    p = DATA / name
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def discover_links(website):
    found = {}
    try:
        html = get(website)
    except Exception as e:
        print(f"  could not read {website}: {e}")
        return found
    for key, pat in SOCIAL_PATTERNS.items():
        hits = re.findall(pat, html)
        if hits:
            # the link that appears most often is usually the outlet's own account
            best = max(set(hits), key=hits.count)
            found[key] = best.rstrip("/")
    return found


channel_cache = load("channel_ids.json", {})


def channel_id(youtube):
    """Turn a YouTube link, @handle or channel id into a channel id (UC...)."""
    if not youtube:
        return None
    youtube = youtube.strip()
    m = re.search(r"(UC[\w\-]{22})", youtube)
    if m:
        return m.group(1)
    if youtube.startswith("@"):
        youtube = "https://www.youtube.com/" + youtube
    if youtube in channel_cache:
        return channel_cache[youtube]
    try:
        html = get(youtube)
        m = (re.search(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w\-]{22})"', html)
             or re.search(r'"externalId":"(UC[\w\-]{22})"', html)
             or re.search(r'"channelId":"(UC[\w\-]{22})"', html))
        if m:
            channel_cache[youtube] = m.group(1)
            return m.group(1)
    except Exception as e:
        print(f"  could not resolve {youtube}: {e}")
    return None


feed_cache = {}


def feed(cid):
    """Newest ~15 uploads of a channel, from YouTube's public RSS feed."""
    if cid in feed_cache:
        return feed_cache[cid]
    items = []
    try:
        root = ET.fromstring(get(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"))
        for e in root.findall("a:entry", ATOM):
            desc = e.find("media:group/media:description", ATOM)
            items.append({
                "v": e.findtext("yt:videoId", "", ATOM),
                "title": e.findtext("a:title", "", ATOM),
                "published": e.findtext("a:published", "", ATOM),
                "desc": (desc.text or "") if desc is not None else "",
            })
    except Exception as e:
        print(f"  feed failed for {cid}: {e}")
    feed_cache[cid] = items
    time.sleep(0.5)
    return items


def mentions(item, names):
    text = f"{item['title']}\n{item['desc']}".lower()
    return any(n.lower() in text for n in names if n.strip())


def main():
    outlets = load("outlets.json", [])
    people = load("people.json", {"people": []}).get("people", [])
    discovered = load("discovered.json", {})

    print("Finding social links...")
    for o in outlets:
        need = [k for k in SOCIAL_PATTERNS if not o.get(k)]
        if need and o.get("website") and not discovered.get(o["id"], {}).get("_checked"):
            print(f"- {o['name']}")
            links = discover_links(o["website"])
            links["_checked"] = datetime.now(timezone.utc).date().isoformat()
            discovered[o["id"]] = links
    (DATA / "discovered.json").write_text(json.dumps(discovered, indent=1))

    def outlet_link(o, key):
        return o.get(key) or discovered.get(o["id"], {}).get(key, "")

    by_id = {o["id"]: o for o in outlets}
    result = {"updated": datetime.now(timezone.utc).isoformat(timespec="minutes"), "people": {}}

    print("Collecting featured people's videos...")
    for p in people:
        pid = p.get("id")
        if not pid:
            continue
        names = p.get("match") or [p.get("name", "")]
        vids = {}
        own = channel_id(p.get("youtube", ""))
        if own:
            for it in feed(own):
                vids[it["v"]] = {**it, "from": "own channel"}
        o = by_id.get(p.get("outlet"))
        if o:
            sc = channel_id(outlet_link(o, "youtube"))
            if sc:
                for it in feed(sc):
                    if it["v"] not in vids and mentions(it, names):
                        vids[it["v"]] = {**it, "from": o.get("short") or o["name"]}
        clean = sorted(vids.values(), key=lambda x: x["published"], reverse=True)[:VIDEOS_PER_PERSON]
        for c in clean:
            c.pop("desc", None)
        result["people"][pid] = clean
        print(f"- {p.get('name')}: {len(clean)} videos")

    (DATA / "videos.json").write_text(json.dumps(result, indent=1))
    (DATA / "channel_ids.json").write_text(json.dumps(channel_cache, indent=1, sort_keys=True))
    print("Done.")


if __name__ == "__main__":
    sys.exit(main())
