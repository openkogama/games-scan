import glob
import html
import json
import os
import re
import sqlite3
import urllib.request

CDN = "https://cdn.openkogama.org/"
DB_URL = "https://github.com/imuarte/kogama-game-list/releases/download/games-v1.0.0/kogama_games_merged.db"
DB = "kogama_games_merged.db"
LISTS = {"games_www.txt": "www", "games_br.txt": "br", "games_friends.txt": "friends", "output.txt": ""}
PAIR = re.compile(r"^(?P<name>.*):(?P<id>[0-9]{2,9})(?::(?P<author>.*))?$")
FIELDS = ("title", "description", "author", "author_id", "likes", "plays", "created_date", "published_date", "scraped_at", "status")


def shards():
    out = {}
    for path in sorted(glob.glob("artifacts/**/shard-*.json", recursive=True)):
        for rec in json.load(open(path, encoding="utf-8")):
            if "error" in rec:
                continue
            old = out.get(rec["sha256"])
            if old:
                old["sources"] = sorted(set(old["sources"]) | set(rec["sources"]))
            else:
                out[rec["sha256"]] = rec
    return out


def lists():
    by_id, names, authors = {}, {}, {}
    for path, site in LISTS.items():
        if not os.path.exists(path):
            continue
        for line in open(path, encoding="utf-8-sig"):
            m = PAIR.match(line.strip())
            if not m:
                continue
            name = html.unescape(m["name"]).strip()
            by_id[m["id"]] = name
            if m["author"]:
                authors[m["id"]] = html.unescape(m["author"]).strip()
            key = (site, name.lower())
            names[key] = None if key in names else m["id"]
    return by_id, names, authors


def database():
    if not os.path.exists(DB):
        urllib.request.urlretrieve(DB_URL, DB)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute("CREATE INDEX IF NOT EXISTS games_id ON games(id)")
    con.execute("CREATE INDEX IF NOT EXISTS games_title ON games(lower(title))")
    return con


def by_title(con, title):
    rows = con.execute("SELECT id FROM games WHERE lower(title) = ?", (title.lower(),)).fetchall()
    return str(rows[0]["id"]) if len(rows) == 1 else ""


def main():
    records = shards()
    listed, by_name, authors = lists()
    con = database()
    rows = {}

    games = []
    for rec in records.values():
        site = rec["region"]
        gid = rec["id"]
        if not gid and rec["fileName"]:
            gid = by_name.get((site, rec["fileName"].lower())) or by_name.get(("", rec["fileName"].lower())) or ""
            if not gid:
                gid = by_title(con, rec["fileName"])
        if gid and gid not in rows:
            row = con.execute("SELECT * FROM games WHERE id = ?", (int(gid),)).fetchone()
            rows[gid] = dict(row) if row else {}
        db = rows.get(gid, {})
        name = rec["name"] or listed.get(gid, "") or (db.get("title") or "") or rec["fileName"]
        games.append(
            {
                "id": gid,
                "site": site,
                "name": name,
                "authorId": str(rec["authorId"] or db.get("author_id") or ""),
                "authorName": authors.get(gid, "") or (db.get("author") or ""),
                "description": db.get("description") or "",
                "likes": db.get("likes"),
                "plays": db.get("plays"),
                "createdDate": db.get("created_date") or "",
                "publishedDate": db.get("published_date") or "",
                "status": db.get("status") or "",
                "scrapedAt": db.get("scraped_at") or "",
                "format": rec["format"],
                "formatVersion": rec["formatVersion"],
                "exporter": rec["exporter"],
                "savedAt": rec["savedAt"],
                "size": rec["size"],
                "sha256": rec["sha256"],
                "urls": [CDN + rec["key"]] + rec["sources"],
            }
        )

    games.sort(key=lambda g: (g["id"] == "", g["id"], g["sha256"]))
    with open("games.json", "w", encoding="utf-8") as f:
        json.dump({"schema": 1, "games": games}, f, indent=4, ensure_ascii=False)
        f.write("\n")

    named = sum(1 for g in games if g["name"])
    known = sum(1 for g in games if g["id"])
    print(f"{len(games)} gier, {known} z id, {named} z nazwa, {len(games) - known} bez id")


if __name__ == "__main__":
    main()
