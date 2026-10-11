import argparse
import glob
import hashlib
import json
import os
import shutil
import sqlite3
import struct
import sys
import zlib
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

from datahash import AVATARS, INDEX, SUPPLEMENT, Reader, batches_of, get
from scan import BUCKET, client

CUBE_MODEL = 1
DEFAULT_CORNERS = 1
ONE_MATERIAL = 2
IDENTITY = bytes([20, 120, 124, 24, 4, 104, 100, 0])
SAMPLES = 5
PRODUCT_SHAPES = 3
MIN_CUBES = 8
TOP_MAPS = 5
MAGIC = b"OKM1"
FORMAT = 3
CARD = struct.Struct("<32sIIfBHIIH")
CARDS_PER_FILE = 16384
BUCKETS = 16
PACKS = 4096
PAGE_SIZE = 4096
KEEP_BUILDS = 2
PREFIX = "models/v2/"
IMMUTABLE = "public, max-age=31536000, immutable"
SHORT = "public, max-age=300"


def build_prefix(build, prefix=PREFIX):
    return f"{prefix}builds/{build}/"


def unique_games():
    games = [g for g in json.loads(get(INDEX))["games"] if g.get("urls") and g.get("sha256")]
    return sorted({g["sha256"]: g for g in games}.values(), key=lambda g: g["sha256"])


def prototypes_and_models(batches):
    prototypes, objects = {}, {}
    for batch in batches:
        r = Reader(batch)
        if len(batch) >= 4 and struct.unpack(">i", batch[:4])[0] == SUPPLEMENT:
            r.i32()
            for _ in range(r.i32()):
                pid, scale, author, size = r.i32(), r.f32(), r.i32(), r.i32()
                prototypes[pid] = (scale, author, r.take(size))
            continue
        for _ in range(r.i32()):
            pid, scale, author, size = r.i32(), r.f32(), r.i32(), r.i32()
            prototypes[pid] = (scale, author, r.take(size))
        for _ in range(r.i32()):
            wid, parent, item, kind = r.i32(), r.i32(), r.i32(), r.i32()
            r.take(40)
            data = dict(r.table())
            flags = r.u8()
            if flags & 1:
                r.i32()
            if flags & 2:
                r.i32()
            r.table()
            objects[wid] = (parent, item, kind, data.get("protoTypeID"))
    return prototypes, objects


def under_avatar(objects):
    children = defaultdict(list)
    for wid, (parent, _, _, _) in objects.items():
        children[parent].append(wid)
    skipped, stack = set(), [wid for wid, obj in objects.items() if obj[2] in AVATARS]
    while stack:
        wid = stack.pop()
        if wid not in skipped:
            skipped.add(wid)
            stack.extend(children[wid])
    return skipped


def cubes_of(data):
    cubes = {}
    count = struct.unpack(">i", data[:4])[0]
    pos = 4
    for _ in range(count):
        x, y, z = struct.unpack(">hhh", data[pos : pos + 6])
        flags = data[pos + 6]
        pos += 7
        if flags & DEFAULT_CORNERS:
            corners = IDENTITY
        else:
            corners = data[pos : pos + 8]
            pos += 8
        if flags & ONE_MATERIAL:
            materials = bytes([data[pos]]) * 6
            pos += 1
        else:
            materials = data[pos : pos + 6]
            pos += 6
        for step in range(max(flags >> 2, 1)):
            cubes[(x + step, y, z)] = (bytes(corners), bytes(materials))
    return cubes


def canonical(cubes):
    out = bytearray(struct.pack(">i", len(cubes)))
    for (x, y, z) in sorted(cubes):
        corners, materials = cubes[(x, y, z)]
        flags = 1 << 2
        if corners == IDENTITY:
            flags |= DEFAULT_CORNERS
        if len(set(materials)) == 1:
            flags |= ONE_MATERIAL
        out += struct.pack(">hhhB", x, y, z, flags)
        if not flags & DEFAULT_CORNERS:
            out += corners
        out += materials[:1] if flags & ONE_MATERIAL else materials
    return bytes(out)


def model_blob(scale, cubes):
    return MAGIC + struct.pack(">f", scale) + canonical(cubes)


def split_blob(blob):
    if blob[:4] != MAGIC:
        return 1.0, blob
    return struct.unpack(">f", blob[4:8])[0], blob[8:]


def models_in(batches):
    prototypes, objects = prototypes_and_models(batches)
    skipped = under_avatar(objects)
    used = defaultdict(lambda: [0, Counter()])
    for wid, (_, item, kind, proto) in objects.items():
        if kind != CUBE_MODEL or wid in skipped or proto not in prototypes:
            continue
        used[proto][0] += 1
        if item > 0:
            used[proto][1][item] += 1

    found = {}
    for proto, (instances, items) in used.items():
        scale, author, data = prototypes[proto]
        cubes = cubes_of(data)
        if len(cubes) < MIN_CUBES:
            continue
        blob = model_blob(scale, cubes)
        sha = hashlib.sha256(blob).hexdigest()
        if sha not in found:
            xs, ys, zs = zip(*cubes)
            materials = Counter()
            for _, faces in cubes.values():
                materials.update(faces)
            found[sha] = {
                "blob": blob,
                "cubes": len(cubes),
                "size": [max(xs) - min(xs) + 1, max(ys) - min(ys) + 1, max(zs) - min(zs) + 1],
                "scale": round(scale, 4),
                "deformed": sum(1 for corners, _ in cubes.values() if corners != IDENTITY),
                "materials": [m for m, _ in materials.most_common(6)],
                "authors": Counter(),
                "items": Counter(),
                "instances": 0,
            }
        found[sha]["authors"][author] += 1
        found[sha]["items"].update(items)
        found[sha]["instances"] += instances
    return found


def read_game(game):
    try:
        return models_in(batches_of(get(game["urls"][0]), game["format"])), None
    except Exception as e:
        return {}, str(e)


def scan(shard, shards, limit):
    games = unique_games()
    mine = list(range(shard, len(games), shards))
    if limit:
        mine = mine[:limit]
    print(f"shard {shard}/{shards}: {len(mine)} of {len(games)}", flush=True)

    folder = f"out/blobs-{shard}"
    os.makedirs(folder, exist_ok=True)
    buckets = [open(os.path.join(folder, f"{bucket:x}.bin"), "wb") for bucket in range(BUCKETS)]
    models, errors = {}, 0
    with ThreadPoolExecutor(8) as pool:
        results = pool.map(lambda index: (index, *read_game(games[index])), mine)
        for done, (index, found, error) in enumerate(results, 1):
            errors += error is not None
            for sha, model in found.items():
                merged = models.get(sha)
                if merged is None:
                    packed = zlib.compress(model["blob"], 9)
                    buckets[int(sha[0], 16)].write(bytes.fromhex(sha) + struct.pack(">I", len(packed)) + packed)
                    merged = models[sha] = {k: v for k, v in model.items() if k != "blob"} | {"games": [], "authors": Counter(), "items": Counter(), "instances": 0}
                merged["games"].append(index)
                merged["authors"].update(model["authors"])
                merged["items"].update(model["items"])
                merged["instances"] += model["instances"]
            if done % 200 == 0:
                print(f"{done}/{len(mine)} maps, {len(models)} models", flush=True)
    for bucket in buckets:
        bucket.close()

    with open(f"out/models-{shard}.json", "w", encoding="utf-8") as f:
        json.dump({"maps": len(mine), "errors": errors, "models": models}, f, separators=(",", ":"))
    print(f"shard {shard}: {len(models)} models, {errors} errors")


def upload(s3, key, data, content_type, cache):
    if s3:
        s3.put_object(Bucket=BUCKET, Key=key, Body=data, ContentType=content_type, CacheControl=cache)


def upload_file(s3, key, path, content_type, cache):
    if s3:
        s3.upload_file(path, BUCKET, key, ExtraArgs={"ContentType": content_type, "CacheControl": cache})


def records(path):
    with open(path, "rb") as f:
        data = f.read()
    pos = 0
    while pos < len(data):
        size = struct.unpack(">I", data[pos + 32 : pos + 36])[0]
        yield data[pos : pos + 32].hex(), data[pos + 36 : pos + 36 + size]
        pos += 36 + size


def pack(folder, build, prefix=PREFIX):
    s3 = client()
    target = build_prefix(build, prefix) + "packs/"
    os.makedirs("out/packs", exist_ok=True)
    per_bucket = PACKS // BUCKETS
    total, written = 0, 0
    with open("out/locations.tsv", "w", encoding="utf-8") as locations, ThreadPoolExecutor(16) as pool:
        sent = []
        for bucket in range(BUCKETS):
            found = {}
            for path in sorted(glob.glob(os.path.join(folder, "**", f"{bucket:x}.bin"), recursive=True)):
                for sha, packed in records(path):
                    found.setdefault(sha, packed)
            grouped = defaultdict(list)
            for sha in sorted(found):
                grouped[int(sha[:3], 16)].append(sha)
            for number in range(bucket * per_bucket, (bucket + 1) * per_bucket):
                if number not in grouped:
                    continue
                body = bytearray()
                for sha in grouped[number]:
                    locations.write(f"{sha}\t{number}\t{len(body)}\t{len(found[sha])}\n")
                    body += found[sha]
                with open(f"out/packs/{number:03x}.bin", "wb") as f:
                    f.write(body)
                sent.append(pool.submit(upload, s3, f"{target}{number:03x}.bin", bytes(body), "application/octet-stream", IMMUTABLE))
                written += len(body)
            total += len(found)
            print(f"bucket {bucket:x}: {len(found)} models", flush=True)
        for future in sent:
            future.result()
    print(f"{total} entries in {len(sent)} packs, {written // 1024 // 1024} MB")


def repaired(text):
    for _ in range(2):
        fixed = None
        for codec in ("cp1252", "latin-1"):
            try:
                fixed = text.encode(codec).decode("utf-8")
                break
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
        if fixed is None or fixed == text:
            break
        text = fixed
    return text


def merged_stats(paths):
    models, maps, errors = {}, 0, 0
    for path in paths:
        with open(path, encoding="utf-8") as f:
            shard = json.load(f)
        maps += shard["maps"]
        errors += shard["errors"]
        for sha, m in shard["models"].items():
            merged = models.get(sha)
            if merged is None:
                merged = models[sha] = {**m, "games": [], "authors": Counter(), "items": Counter(), "instances": 0}
            merged["games"].extend(m["games"])
            merged["authors"].update({int(k): v for k, v in m["authors"].items()})
            merged["items"].update({int(k): v for k, v in m["items"].items()})
            merged["instances"] += m["instances"]
        del shard
    return models, maps, errors


SCHEMA = """
CREATE TABLE models (
    id INTEGER PRIMARY KEY,
    sha256 TEXT NOT NULL,
    maps INTEGER NOT NULL,
    instances INTEGER NOT NULL,
    cubes INTEGER NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    depth INTEGER NOT NULL,
    scale REAL NOT NULL,
    deformed INTEGER NOT NULL,
    materials TEXT NOT NULL,
    image INTEGER NOT NULL,
    pack INTEGER NOT NULL,
    offset INTEGER NOT NULL,
    length INTEGER NOT NULL
);
CREATE TABLE maps (id INTEGER PRIMARY KEY, site TEXT NOT NULL, game TEXT NOT NULL, name TEXT NOT NULL);
CREATE TABLE model_maps (model INTEGER NOT NULL, map INTEGER NOT NULL, PRIMARY KEY (model, map)) WITHOUT ROWID;
CREATE TABLE model_authors (model INTEGER NOT NULL, author INTEGER NOT NULL, PRIMARY KEY (model, author)) WITHOUT ROWID;
CREATE TABLE model_items (model INTEGER NOT NULL, item INTEGER NOT NULL, PRIMARY KEY (model, item)) WITHOUT ROWID;
CREATE VIRTUAL TABLE map_search USING fts5(name, content='maps', content_rowid='id');
"""

INDEXES = """
CREATE UNIQUE INDEX models_sha ON models(sha256);
CREATE INDEX models_scale ON models(scale, id);
CREATE INDEX model_maps_map ON model_maps(map, model);
CREATE INDEX model_authors_author ON model_authors(author, model);
CREATE INDEX model_items_item ON model_items(item, model);
INSERT INTO map_search(map_search) VALUES ('rebuild');
"""


def search_database(words):
    if os.path.exists("out/search.db"):
        os.remove("out/search.db")
    db = sqlite3.connect("out/search.db")
    db.execute(f"PRAGMA page_size = {PAGE_SIZE}")
    db.execute("PRAGMA journal_mode = DELETE")
    db.execute("CREATE VIRTUAL TABLE model_search USING fts5(words, content='', detail='none')")
    db.executemany("INSERT INTO model_search(rowid, words) VALUES (?, ?)", words)
    db.execute("INSERT INTO model_search(model_search) VALUES ('optimize')")
    db.commit()
    db.execute("VACUUM")
    db.close()


def scale_word(scale):
    return "zzscale" + f"{scale:g}".replace(".", "")


def index(paths, build):
    games = unique_games()
    models, maps, errors = merged_stats(paths)
    with open("out/locations.tsv", encoding="utf-8") as f:
        locations = {sha: (int(number), int(offset), int(length)) for sha, number, offset, length in (line.split("\t") for line in f)}

    shapes = defaultdict(set)
    for sha, m in models.items():
        for item in m["items"]:
            shapes[item].add(sha)
    products = {item for item, shas in shapes.items() if len(shas) <= PRODUCT_SHAPES}

    order = sorted((sha for sha in models if sha in locations),
                   key=lambda sha: (len(set(models[sha]["games"])), models[sha]["instances"]), reverse=True)
    if os.path.exists("out/models.db"):
        os.remove("out/models.db")
    db = sqlite3.connect("out/models.db")
    db.execute(f"PRAGMA page_size = {PAGE_SIZE}")
    db.execute("PRAGMA journal_mode = DELETE")
    db.executescript(SCHEMA)

    names = {}

    def name(game):
        if game not in names:
            names[game] = repaired(games[game].get("name") or "")
        return names[game]

    top, used_games, first_games = [], set(), set()
    rows, links, authors, items, words = [], [], [], [], []
    cards = bytearray()
    for rank, sha in enumerate(order, 1):
        m = models[sha]
        map_count = len(set(m["games"]))
        number, offset, length = locations[sha]
        image = map_count >= TOP_MAPS
        rows.append((rank, sha, map_count, m["instances"], m["cubes"], *m["size"], m["scale"], m["deformed"],
                     ",".join(str(x) for x in m["materials"]), int(image), number, offset, length))
        samples = list(dict.fromkeys(m["games"]))[:SAMPLES]
        used_games.update(samples)
        titled = next((game for game in samples if name(game)), samples[0] if samples else None)
        if titled is not None:
            first_games.add(titled)
        cards += CARD.pack(bytes.fromhex(sha), map_count, m["cubes"], m["scale"], int(image), number, offset, length, 0 if titled is None else titled + 1)
        words.append((rank, " ".join([name(game) for game in samples] + [scale_word(m["scale"])])))
        links.extend((rank, game + 1) for game in samples)
        authors.extend((rank, author) for author, _ in m["authors"].most_common(SAMPLES))
        items.extend((rank, item) for item, _ in m["items"].most_common() if item in products)
        if image:
            top.append({"sha256": sha, "pack": number, "offset": offset, "length": length})
    db.executemany("INSERT INTO models VALUES (" + ",".join("?" * 15) + ")", rows)
    db.executemany("INSERT INTO maps VALUES (?, ?, ?, ?)",
                   [(game + 1, games[game].get("site") or "", games[game].get("id") or "", name(game)) for game in sorted(used_games)])
    db.executemany("INSERT INTO model_maps VALUES (?, ?)", links)
    db.executemany("INSERT INTO model_authors VALUES (?, ?)", authors)
    db.executemany("INSERT INTO model_items VALUES (?, ?)", items)
    db.executescript(INDEXES)
    db.commit()
    db.execute("ANALYZE")
    db.execute("VACUUM")
    db.close()
    search_database(words)
    shutil.rmtree("out/cards", ignore_errors=True)
    os.makedirs("out/cards")
    chunk = CARDS_PER_FILE * CARD.size
    for number, start in enumerate(range(0, len(cards), chunk)):
        with open(f"out/cards/{number:03x}.bin", "wb") as f:
            f.write(cards[start : start + chunk])
    titles = [""] * (max(first_games, default=-1) + 2)
    for game in first_games:
        titles[game + 1] = name(game)
    with open("out/maps.json", "w", encoding="utf-8") as f:
        json.dump(titles, f, ensure_ascii=False, separators=(",", ":"))

    s3 = client()
    base = build_prefix(build)
    upload_file(s3, base + "database.bin", "out/models.db", "application/octet-stream", IMMUTABLE)
    upload_file(s3, base + "search.bin", "out/search.db", "application/octet-stream", IMMUTABLE)
    for name in sorted(os.listdir("out/cards")):
        upload_file(s3, base + "cards/" + name, os.path.join("out/cards", name), "application/octet-stream", IMMUTABLE)
    upload_file(s3, base + "maps.json", "out/maps.json", "application/json; charset=utf-8", IMMUTABLE)
    materials = {}
    if os.path.exists("out/materials/materials.json"):
        with open("out/materials/materials.json", encoding="utf-8") as f:
            materials = json.load(f)
        upload_file(s3, base + "atlas.png", "out/materials/atlas.png", "image/png", IMMUTABLE)

    latest = {
        "format": FORMAT,
        "build": build,
        "maps": maps,
        "errors": errors,
        "models": len(order),
        "images": len(top),
        "packs": PACKS,
        "database": {"path": "database.bin", "bytes": os.path.getsize("out/models.db"), "pageSize": PAGE_SIZE},
        "search": {"path": "search.bin", "bytes": os.path.getsize("out/search.db"), "pageSize": PAGE_SIZE},
        "cards": {"path": "cards/", "record": CARD.size, "perFile": CARDS_PER_FILE},
        "titles": "maps.json",
        "materials": {key: value for key, value in materials.items() if key != "colors"},
    }
    with open("out/latest.json", "w", encoding="utf-8") as f:
        json.dump(latest, f)
    with open("out/top.json", "w", encoding="utf-8") as f:
        json.dump({"build": build, "models": top}, f)
    print(f"{maps} maps, {errors} errors, {len(order)} models, {len(top)} with images, database {latest['database']['bytes'] // 1024 // 1024} MB")


def publish(prefix=PREFIX):
    s3 = client()
    with open("out/latest.json", "rb") as f:
        upload(s3, prefix + "latest.json", f.read(), "application/json", SHORT)
    if not s3:
        return
    builds = defaultdict(lambda: [None, []])
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix + "builds/"):
        for item in page.get("Contents", []):
            build = item["Key"][len(prefix + "builds/") :].split("/")[0]
            entry = builds[build]
            entry[0] = max(entry[0], item["LastModified"]) if entry[0] else item["LastModified"]
            entry[1].append(item["Key"])
    old = sorted(builds, key=lambda build: builds[build][0], reverse=True)[KEEP_BUILDS:]
    keys = [key for build in old for key in builds[build][1]]
    for start in range(0, len(keys), 1000):
        s3.delete_objects(Bucket=BUCKET, Delete={"Objects": [{"Key": key} for key in keys[start : start + 1000]]})
    print(f"published, removed {len(old)} old builds")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser()
    p.add_argument("--shard", type=int, default=0)
    p.add_argument("--shards", type=int, default=1)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--build", default="local")
    p.add_argument("--pack", default="")
    p.add_argument("--index", nargs="*")
    p.add_argument("--publish", action="store_true")
    args = p.parse_args()
    if args.pack:
        pack(args.pack, args.build)
    elif args.index is not None:
        index(args.index, args.build)
    elif args.publish:
        publish()
    else:
        scan(args.shard, args.shards, args.limit)
