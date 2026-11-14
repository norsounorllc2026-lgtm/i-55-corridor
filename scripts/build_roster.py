#!/usr/bin/env python3
"""I-55 Corridor roster builder.

Runs weekly from GitHub Actions (and whenever you start it by hand). For every
newsroom in data/outlets.json it:
  1. finds the newsroom's team page (anchors, reporters, staff, hosts),
  2. opens each person's bio page on the newsroom's own site,
  3. records their name, role, photo, bio link, and any personal YouTube,
     Facebook, Instagram, X, TikTok or LinkedIn accounts linked there.

People who have at least one personal social account are marked
"posts": true. Those are the ones who post on their own, and the ones the
picker (picker.html) shows first.

The result goes to data/roster.json. Nothing here changes the map; the map only
shows the people you choose in data/people.json.
Standard library only.
"""
import json
import re
import sys
import time
import html as htmllib
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
UA = "Mozilla/5.0 (compatible; I55CorridorBot/1.0)"
MAX_PEOPLE = 60          # per newsroom
PAUSE = 0.4              # seconds between requests to the same site

TEAM_PATHS = [
    "/meet-the-team/", "/news-team/", "/news-team", "/about-us/meet-the-team/", "/about-us/news-team/",
    "/section/team-bios", "/team-bios", "/our-team/", "/team/", "/staff/", "/about/staff/", "/about-us/staff/",
    "/people/", "/about/people/", "/meet-our-team/", "/on-air/", "/personalities/", "/hosts/", "/about-us/",
    "/about/", "/contact-us/", "/contact/",
]
TEAM_WORDS = re.compile(r"\b(meet (the|our) (team|staff)|news team|our team|on[- ]air|team bios|anchors|staff|personalities|hosts|our people|reporters|newsroom)\b", re.I)
PROFILE_PATTERNS = re.compile(
    r"/(authors?|people|person|staff|team-bios|team|talent|bios?|personalities|hosts?|profiles?|contributors?|reporters?|meet-the-team|news-team|people-directory)/[^\"'#?]+", re.I)
SOCIAL = {
    "youtube": r"https?://(?:www\.)?youtube\.com/(?:@[\w.\-]+|channel/UC[\w\-]{22}|c/[\w.\-]+|user/[\w.\-]+)",
    "facebook": r"https?://(?:www\.)?facebook\.com/(?!sharer|share|dialog|plugins|tr\b|groups/)[\w.\-]+/?",
    "instagram": r"https?://(?:www\.)?instagram\.com/(?!p/|reel/|explore/)[\w.\-]+/?",
    "x": r"https?://(?:www\.)?(?:twitter|x)\.com/(?!intent|share|home|search)[\w]+/?",
    "tiktok": r"https?://(?:www\.)?tiktok\.com/@[\w.\-]+/?",
    "linkedin": r"https?://(?:www\.)?linkedin\.com/in/[\w\-%]+/?",
}
NOT_A_PERSON = re.compile(r"\b(team|staff|contact|careers?|jobs|advertis|privacy|terms|newsletter|subscribe|about us|weather|traffic|sports|news|home|category|tag)\b", re.I)


def get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if r.status >= 400:
            raise IOError(r.status)
        return r.geturl(), r.read(2_500_000).decode("utf-8", "replace")


def load(name, default):
    try:
        return json.loads((DATA / name).read_text())
    except Exception:
        return default


def links_in(page_url, html):
    out = []
    for m in re.finditer(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.I | re.S):
        href = htmllib.unescape(m.group(1)).strip()
        text = re.sub(r"<[^>]+>", " ", m.group(2))
        text = htmllib.unescape(re.sub(r"\s+", " ", text)).strip()
        if href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        out.append((urllib.parse.urljoin(page_url, href), text))
    return out


def same_site(a, b):
    ha, hb = urllib.parse.urlparse(a).netloc.lower(), urllib.parse.urlparse(b).netloc.lower()
    strip = lambda h: h[4:] if h.startswith("www.") else h
    return strip(ha) == strip(hb) or strip(ha).endswith("." + strip(hb)) or strip(hb).endswith("." + strip(ha))


def socials_in(html):
    found = {}
    for key, pat in SOCIAL.items():
        hits = [h.rstrip("/") for h in re.findall(pat, html)]
        if hits:
            found[key] = hits
    return found


def meta(html, prop):
    m = (re.search(r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]+content=["\']([^"\']*)' % re.escape(prop), html, re.I)
         or re.search(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']%s["\']' % re.escape(prop), html, re.I))
    return htmllib.unescape(m.group(1)).strip() if m else ""


def clean_name(title, site_name):
    t = re.split(r"\s+[|\-–—]\s+", title)[0].strip()
    t = re.sub(r"^(meet|about)\s+", "", t, flags=re.I)
    if site_name and site_name.lower() in t.lower():
        t = re.sub(re.escape(site_name), "", t, flags=re.I).strip(" -|")
    return t


def looks_like_name(s):
    words = s.split()
    return 2 <= len(words) <= 5 and all(w[0].isupper() for w in words if w[0].isalpha()) and not NOT_A_PERSON.search(s) and len(s) < 48


def find_team_pages(website):
    pages = []
    try:
        url, html = get(website)
        for href, text in links_in(url, html):
            if same_site(href, website) and TEAM_WORDS.search(text or "") and href not in pages:
                pages.append(href)
    except Exception as e:
        print(f"    homepage failed: {e}")
    base = "{0.scheme}://{0.netloc}".format(urllib.parse.urlparse(website))
    for p in TEAM_PATHS:
        u = base + p
        if u not in pages:
            pages.append(u)
    return pages


def profile_links(team_url, html, website):
    seen, out = set(), []
    for href, text in links_in(team_url, html):
        if not same_site(href, website):
            continue
        path = urllib.parse.urlparse(href).path
        if not PROFILE_PATTERNS.search(path):
            continue
        if path.rstrip("/") == urllib.parse.urlparse(team_url).path.rstrip("/"):
            continue
        key = href.split("#")[0].split("?")[0].rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        out.append((key, text))
    return out


def build_for(outlet, outlet_socials):
    website = outlet.get("website")
    if not website:
        return {"team_pages": [], "people": [], "note": "no website"}
    team_pages, candidates = [], []
    for page in find_team_pages(website):
        try:
            url, html = get(page)
        except Exception:
            continue
        links = profile_links(url, html, website)
        if len(links) >= 2:
            team_pages.append(url)
            candidates.extend(l for l in links if l[0] not in {c[0] for c in candidates})
        if len(candidates) >= MAX_PEOPLE or len(team_pages) >= 3:
            break
        time.sleep(PAUSE)
    people = []
    own = {v.lower() for vals in outlet_socials.values() for v in (vals if isinstance(vals, list) else [vals]) if v}
    for href, text in candidates[:MAX_PEOPLE]:
        try:
            url, html = get(href)
        except Exception:
            continue
        time.sleep(PAUSE)
        site_name = meta(html, "og:site_name")
        name = text if looks_like_name(text) else clean_name(meta(html, "og:title") or re.sub(r"<[^>]+>", "", (re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S | re.I) or [None, ""])[1]), site_name)
        name = htmllib.unescape(re.sub(r"\s+", " ", name)).strip()
        if not looks_like_name(name):
            continue
        desc = meta(html, "og:description") or meta(html, "description")
        role_m = re.search(r"\b(chief meteorologist|meteorologist|anchor(?:/reporter)?|co-anchor|reporter|multimedia journalist|mmj|correspondent|host|producer|editor|columnist|photographer|photojournalist|sports director|sports anchor|investigative reporter|news director|journalist|writer)\b", desc + " " + html[:20000], re.I)
        person = {
            "key": re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") + "-" + outlet["id"],
            "name": name,
            "role": role_m.group(1).title().replace("Mmj", "MMJ") if role_m else "",
            "photo": meta(html, "og:image"),
            "profile": url,
        }
        # personal accounts: links on the bio page that aren't the newsroom's own accounts
        counts = socials_in(html)
        for key, hits in counts.items():
            personal = [h for h in hits if h.lower() not in own and hits.count(h) <= 3]
            if personal:
                # prefer a handle that resembles the person's name
                last = name.split()[-1].lower()
                personal.sort(key=lambda h: (last not in h.lower(), personal.count(h) * -1))
                person[key] = personal[0]
        person["posts"] = any(person.get(k) for k in SOCIAL)
        people.append(person)
    people.sort(key=lambda p: (not p["posts"], p["name"]))
    return {"team_pages": team_pages, "people": people}


def main():
    only = set(sys.argv[1:])
    outlets = load("outlets.json", [])
    discovered = load("discovered.json", {})
    roster = load("roster.json", {})
    for o in outlets:
        if only and o["id"] not in only:
            continue
        print(f"- {o['name']}")
        own = {k: o.get(k) or discovered.get(o["id"], {}).get(k, "") for k in SOCIAL}
        try:
            result = build_for(o, own)
        except Exception as e:
            print(f"    failed: {e}")
            continue
        result["checked"] = datetime.now(timezone.utc).date().isoformat()
        print(f"    {len(result['people'])} people, {sum(p['posts'] for p in result['people'])} post on their own")
        roster[o["id"]] = result
        (DATA / "roster.json").write_text(json.dumps(roster, indent=1))
    print("Done.")


if __name__ == "__main__":
    main()
