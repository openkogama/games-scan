import argparse
import glob
import html
import json
import os
import shutil
import sys

from models import IMMUTABLE, SHORT, build_prefix, publish, repaired, unique_games, upload_file
from scan import client

PREFIX = "maps/v1/"
FORMAT = 1
DETAILS = ["description", "created", "published", "versions"]
VERSION = ["sha256", "format", "size", "savedAt", "exporter", "urls"]
PER_FILE = 1024
HASH_PREFIX = 2
FIELDS = ["id", "site", "name", "author", "authorId", "plays", "likes", "published", "format", "size", "versions", "image"]


def unmangled(value):
    for _ in range(3):
        raw = bytearray()
        for c in value:
            try:
                raw += c.encode("cp1252")
            except UnicodeEncodeError:
                if ord(c) > 255:
                    return value
                raw.append(ord(c))
        try:
            fixed = raw.decode("utf-8")
        except UnicodeDecodeError:
            return value
        if fixed == value:
            return value
        value = fixed
    return value


def text(value):
    value = unmangled(repaired(str(value or "")))
    for _ in range(3):
        value = html.unescape(value)
    return " ".join(value.split())


def number(value):
    return value if isinstance(value, int) else None


def primary(game):
    return (game.get("exporter") != "", game.get("savedAt") or "", game.get("format") == "kgmap", game.get("size") or 0)


def grouped(games):
    groups, by_key = [], {}
    for game in games:
        name = text(game.get("name")).casefold()
        key = (game.get("id"), name) if game.get("id") else ("sha", game["sha256"])
        if key not in by_key:
            by_key[key] = len(groups)
            groups.append([])
        groups[by_key[key]].append(game)
    return [sorted(group, key=primary, reverse=True) for group in groups]


def pick(group, field):
    return next((game.get(field) for game in group if game.get(field) not in ("", None)), None)


def thumbnails(s3):
    from thumbs import load_state

    images = {}
    for path in glob.glob("out/thumbs-*.json"):
        for key, value in json.load(open(path)).items():
            if value.get("source"):
                images[key] = value["image"]
    for key, value in load_state(s3)[1].items():
        if value.get("source"):
            images[key] = value["image"]
    return images


def index(build, source):
    s3 = client()
    images = thumbnails(s3)
    games = unique_games() if source is None else sorted({g["sha256"]: g for g in json.load(open(source, encoding="utf-8"))["games"] if g.get("urls") and g.get("sha256")}.values(), key=lambda g: g["sha256"])
    groups = grouped(games)
    groups.sort(key=lambda group: (-(pick(group, "plays") or -1), text(group[0].get("name")).casefold()))
    rows, details, hashes = [], [], {}
    for group in groups:
        best = group[0]
        image = images.get(f"{pick(group, 'site') or ''}:{pick(group, 'id') or ''}", "")
        for game in group:
            hashes.setdefault(game["sha256"][:HASH_PREFIX], {})[game["sha256"]] = [len(rows), image]
        rows.append([
            pick(group, "id") or "",
            pick(group, "site") or "",
            text(pick(group, "name")),
            text(pick(group, "authorName")),
            str(pick(group, "authorId") or ""),
            number(pick(group, "plays")),
            number(pick(group, "likes")),
            (pick(group, "publishedDate") or pick(group, "createdDate") or "")[:10],
            best.get("format") or "",
            best.get("size") or 0,
            len(group),
            image,
        ])
        details.append([
            text(pick(group, "description")),
            pick(group, "createdDate") or "",
            pick(group, "publishedDate") or "",
            [[g["sha256"], g.get("format") or "", g.get("size") or 0, g.get("savedAt") or "", g.get("exporter") or "", g.get("urls") or []] for g in group],
        ])

    os.makedirs("out/maps/details", exist_ok=True)
    shutil.rmtree("out/maps/hashes", ignore_errors=True)
    os.makedirs("out/maps/hashes")
    for prefix, entries in hashes.items():
        with open(f"out/maps/hashes/{prefix}.json", "w", encoding="utf-8") as f:
            json.dump(entries, f, separators=(",", ":"))
    with open("out/maps/index.json", "w", encoding="utf-8") as f:
        json.dump({"fields": FIELDS, "rows": rows}, f, ensure_ascii=False, separators=(",", ":"))
    for number_, start in enumerate(range(0, len(details), PER_FILE)):
        with open(f"out/maps/details/{number_:03x}.json", "w", encoding="utf-8") as f:
            json.dump(details[start : start + PER_FILE], f, ensure_ascii=False, separators=(",", ":"))

    latest = {
        "format": FORMAT,
        "build": build,
        "maps": len(rows),
        "files": len(games),
        "index": {"path": "index.json", "bytes": os.path.getsize("out/maps/index.json")},
        "images": {"path": "../../images/", "count": sum(1 for row in rows if row[-1])},
        "details": {"path": "details/", "perFile": PER_FILE, "fields": DETAILS, "version": VERSION},
        "hashes": {"path": "hashes/", "prefix": HASH_PREFIX, "value": ["position", "image"]},
    }
    with open("out/maps/latest.json", "w", encoding="utf-8") as f:
        json.dump(latest, f)

    if os.path.exists("captures.json.gz"):
        upload_file(s3, PREFIX + "captures.json.gz", "captures.json.gz", "application/gzip", SHORT)
    base = build_prefix(build, PREFIX)
    upload_file(s3, base + "index.json", "out/maps/index.json", "application/json; charset=utf-8", IMMUTABLE)
    for name in sorted(os.listdir("out/maps/details")):
        upload_file(s3, base + "details/" + name, os.path.join("out/maps/details", name), "application/json; charset=utf-8", IMMUTABLE)
    for name in sorted(os.listdir("out/maps/hashes")):
        upload_file(s3, base + "hashes/" + name, os.path.join("out/maps/hashes", name), "application/json; charset=utf-8", IMMUTABLE)
    print(f"{len(rows)} maps, index {latest['index']['bytes'] // 1024} KB, {len(os.listdir('out/maps/details'))} detail files")


def publish_maps():
    os.makedirs("out", exist_ok=True)
    with open("out/maps/latest.json", "rb") as src, open("out/latest.json", "wb") as dst:
        dst.write(src.read())
    publish(PREFIX)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser()
    p.add_argument("--build", default="local")
    p.add_argument("--source", help="local games.json instead of the cdn copy")
    p.add_argument("--publish", action="store_true")
    args = p.parse_args()
    if args.publish:
        publish_maps()
    else:
        index(args.build, args.source)
