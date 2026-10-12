import argparse
import glob
import gzip
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

WAYBACK = "https://web.archive.org/"
HOSTS = {"www": "www.kogama.com", "br": "kogama.com.br", "friends": "friends.kogama.com"}
STATIC = {"www": "www.kogstatic.com", "br": "br.kogstatic.com", "friends": "friends.kogstatic.com"}
BUCKETS = {
    "www": "https://s3.eu-west-1.amazonaws.com/eu.kogama.com/",
    "br": "https://s3.sa-east-1.amazonaws.com/br.kogama.com/",
    "friends": "https://s3.amazonaws.com/us.kogama.com/",
}
SIZES = ("350x194", "184x100", "600x240")
UUID = r"([0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12})"
GAME = r"_(?:184x100|350x194|600x240|350x175)"


DEADLINE = float("inf")


def get(url, attempts=6, timeout=120):
    for attempt in range(attempts):
        if time.time() > DEADLINE:
            return None
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "openkogama-games-scan"})
            with urllib.request.urlopen(req, timeout=min(timeout, max(5, DEADLINE - time.time()))) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                return None
        except Exception:
            pass
        time.sleep(max(0, min(120, 10 * 2 ** attempt, DEADLINE - time.time())))
    return None


def cdx_pages(prefix):
    text = get(f"{WAYBACK}cdx/search/cdx?url={prefix}&matchType=prefix&showNumPages=true")
    return int(text.strip()) if text and text.strip().isdigit() else 0


def captures(shard, shards):
    jobs = []
    for site, host in HOSTS.items():
        jobs += [(site, host, "games/play", page) for page in range(cdx_pages(f"{host}/games/play/"))]
        jobs += [(site, host, "profile", page) for page in range(cdx_pages(f"{host}/profile/"))]
    found = {}
    for site, host, path, page in jobs[shard::shards]:
        text = get(f"{WAYBACK}cdx/search/cdx?url={host}/{path}/&matchType=prefix&fl=timestamp,original,mimetype&filter=statuscode:200&page={page}")
        for line in (text or b"").decode("utf-8", "ignore").splitlines():
            parts = line.split()
            if len(parts) < 3 or "html" not in parts[2]:
                continue
            address = parts[1].split("?")[0]
            m = re.match(r"https?://[^/]+/games/play/(\d+)/?$", address)
            p = re.match(r"https?://[^/]+/profile/(\d+)/games/?$", address)
            if m or p:
                key = f"{site}:{m.group(1)}" if m else f"{site}:profile:{p.group(1)}"
                if parts[0] > found.get(key, ["", ""])[0]:
                    found[key] = [parts[0], parts[1]]
        print(f"{site} {path} page {page}: {len(found)}", flush=True)
        time.sleep(2)
    os.makedirs("out", exist_ok=True)
    with open(f"out/captures-{shard}.json", "w") as f:
        json.dump(found, f)


def enclosing_id(html, pos):
    depth, i = 0, pos
    while i > 0:
        i -= 1
        if html[i] == "}":
            depth += 1
        elif html[i] == "{":
            if depth == 0:
                break
            depth -= 1
    depth, j = 0, pos
    while j < len(html):
        if html[j] == "{":
            depth += 1
        elif html[j] == "}":
            if depth == 0:
                break
            depth -= 1
        j += 1
    flat, depth = [], 0
    for c in html[i + 1 : j]:
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif depth == 0:
            flat.append(c)
    m = re.search(r'"id":\s*(\d+)', "".join(flat))
    return m.group(1) if m else None


def pairs(html, page_id):
    out = {}
    for m in re.finditer(r'"images":\s*\{', html):
        image = re.search(r"gen_cache/[0-9a-f]{2}/[0-9a-f]{2}/" + UUID + GAME, html[m.end() : m.end() + 600])
        game = image and enclosing_id(html, m.start())
        if game:
            out.setdefault(game, image.group(1))
    for m in re.finditer(r'href="[^"]*/games/play/(\d+)/?"[^>]*>(.{0,500}?)gen_cache/[0-9a-f]{2}/[0-9a-f]{2}/' + UUID + GAME, html, re.S):
        if "/games/play/" not in m.group(2):
            out.setdefault(m.group(1), m.group(3))
    m = re.search(r'og:image" content="[^"]*gen_cache/[0-9a-f]{2}/[0-9a-f]{2}/' + UUID + GAME, html)
    if m:
        out.setdefault(page_id, m.group(1))
    return out


def key_of(uuid, size):
    plain = uuid.replace("-", "")
    return f"gen_cache/{plain[:2]}/{plain[2:4]}/{uuid}_{size}.jpg"


def image_of(site, uuid):
    for size in SIZES:
        data = get(BUCKETS[site] + key_of(uuid, size), attempts=2)
        if data:
            return data, "bucket"
    prefix = f"{STATIC[site]}/{key_of(uuid, '')[:-5]}"
    text = get(f"{WAYBACK}cdx/search/cdx?url={prefix}&matchType=prefix&filter=statuscode:200&fl=timestamp,original", attempts=3)
    captures = [line.split()[:2] for line in (text or b"").decode().splitlines() if len(line.split()) >= 2]
    for size in SIZES:
        for ts, original in captures:
            if f"_{size}.jpg" in original:
                data = get(f"{WAYBACK}web/{ts}id_/{original}", attempts=3)
                if data and data[:2] == b"\xff\xd8":
                    return data, "wayback"
    return None, None


def wanted_maps(index_path):
    rows = json.load(open(index_path, encoding="utf-8"))["rows"]
    wanted, authors = set(), {}
    for r in rows:
        if r[0] and r[1] in HOSTS:
            wanted.add(f"{r[1]}:{r[0]}")
            if r[4]:
                authors.setdefault(f"{r[1]}:profile:{r[4]}", set()).add(f"{r[1]}:{r[0]}")
    return wanted, authors


def load_captures():
    captured = {}
    for path in glob.glob("captures.json.gz") + glob.glob("captures/*.json"):
        data = json.load(gzip.open(path) if path.endswith(".gz") else open(path))
        for key, value in data.items():
            if value[0] > captured.get(key, ["", ""])[0]:
                captured[key] = value
    return captured


STATE = "maps/v1/thumbs/"
IMAGES = "maps/v1/images/"


def load_state(s3):
    pages, stored = set(), {}
    if not s3:
        return pages, stored
    from scan import BUCKET

    for listing in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=STATE):
        for item in listing.get("Contents", []):
            state = json.loads(s3.get_object(Bucket=BUCKET, Key=item["Key"])["Body"].read())
            pages.update(state["pages"])
            for key, value in state["stored"].items():
                if value.get("source") or key not in stored:
                    stored[key] = value
    return pages, stored


def fetch(shard, shards, index_path, limit, minutes):
    from threading import Lock

    from models import IMMUTABLE, SHORT, upload
    from scan import client

    global DEADLINE
    deadline = DEADLINE = time.time() + minutes * 60
    s3 = client()
    captured = load_captures()
    wanted, authors = wanted_maps(index_path)
    done_pages, stored = load_state(s3)
    pages = {k for k in wanted if k in captured}
    pages |= {p for p, maps in authors.items() if p in captured and not maps <= pages}
    jobs = [k for k in sorted(pages) if k not in done_pages][shard::shards]
    if limit:
        jobs = jobs[:limit]
    print(f"shard {shard}/{shards}: {len(jobs)} pages left, {len(done_pages)} done, {sum(1 for v in stored.values() if v.get('source'))} images stored", flush=True)
    folder = f"out/thumbs-{shard}"
    os.makedirs(folder, exist_ok=True)
    lock = Lock()
    mine = {"pages": [], "stored": {}}
    saved = [time.time()]

    def save(force=False):
        with lock:
            if not force and time.time() - saved[0] < 300:
                return
            saved[0] = time.time()
            data = json.dumps(mine).encode()
        upload(s3, f"{STATE}shard-{shard}-of-{shards}.json", data, "application/json", SHORT)
        with open(f"out/thumbs-{shard}.json", "w") as f:
            json.dump(mine["stored"], f)

    def store(key, uuid):
        with lock:
            if key in stored and (stored[key].get("source") or stored[key]["image"] == uuid):
                return
            stored[key] = {"image": uuid, "source": None}
        data, source = image_of(key.split(":")[0], uuid)
        if data:
            path = os.path.join(folder, uuid + ".jpg")
            with open(path, "wb") as f:
                f.write(data)
            upload(s3, f"{IMAGES}{uuid}.jpg", data, "image/jpeg", IMMUTABLE)
        with lock:
            stored[key] = mine["stored"][key] = {"image": uuid, "source": source}

    def page(key):
        if time.time() > deadline:
            return
        site, *rest = key.split(":")
        game = rest[-1] if len(rest) == 1 else None
        ts, url = captured[key]
        html = get(f"{WAYBACK}web/{ts}id_/{url}")
        links = pairs(html.decode("utf-8", "ignore"), game) if html else {}
        own = f"{site}:{game}" if game else None
        for other, uuid in sorted(links.items(), key=lambda item: f"{site}:{item[0]}" != own):
            if f"{site}:{other}" in wanted and time.time() < deadline:
                store(f"{site}:{other}", uuid)
        if time.time() < deadline:
            with lock:
                mine["pages"].append(key)
        save()

    with ThreadPoolExecutor(4) as pool:
        for done, _ in enumerate(pool.map(page, jobs), 1):
            if done % 100 == 0:
                print(f"{done}/{len(jobs)} pages, {len(mine['stored'])} images this run", flush=True)
    save(force=True)
    counts = {}
    for value in mine["stored"].values():
        counts[value["source"]] = counts.get(value["source"], 0) + 1
    print(f"shard {shard}: {len(mine['pages'])} pages, {len(mine['stored'])} images {counts}", flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser()
    p.add_argument("--captures", action="store_true")
    p.add_argument("--fetch", action="store_true")
    p.add_argument("--index", default="out/maps/index.json")
    p.add_argument("--shard", type=int, default=0)
    p.add_argument("--shards", type=int, default=1)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--minutes", type=float, default=240)
    args = p.parse_args()
    if args.captures:
        captures(args.shard, args.shards)
    else:
        fetch(args.shard, args.shards, args.index, args.limit, args.minutes)
