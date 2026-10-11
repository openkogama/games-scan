import argparse
import hashlib
import json
import math
import os
import shutil
import sqlite3
import struct
import sys
import zlib
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from datahash import batches_of, get, parse
from models import (BUCKETS, IMMUTABLE, PACKS, PAGE_SIZE, build_prefix, canonical, client, cubes_of, model_blob, pack, publish,
                    repaired, unique_games, upload_file)

PREFIX = "avatars/v1/"
MAGIC = b"OKA1"
FORMAT = 1
CUBE_MODEL = 1
AVATAR = 0
BLUEPRINT = 45
BUILD_AVATAR = 133
CLASS = 134
PARTS = ("Head", "Torso", "LArm", "RArm", "LUpLeg", "LLowLeg", "RUpLeg", "RLowLeg")
TURNED = {"Torso": (180, (-0.5, 1.5)), "LUpLeg": (-90, None), "RUpLeg": (-90, None)}
SOURCES = {"map": 1, "build": 2, "class": 4, "world": 8, "placed": 16}
MARKERS = {
    "f85caecbd091ecd9a2f1ae396e1f8c03b59f2f262e5a99433f08133bb2dd46ff",
    "49d779bfda6cb47abe215cc81060774a4949a27c9a3206a3e61ef2cdc760c538",
    "63ca06ea413f344bd9444eae72e018b55869854bc535f10ad5ca296199e5023b",
    "ef5058045d65b739a46442a8d59615834d981619b0ed5e5bf344ebee65aafabc",
    "9ab365f5c0916cc0ff10a23c2ed25ec6209ea996e613c1561415fdf770960448",
    "9cca846cd04efa1ed3a66a908e1d3e3e691916dea63ca71f3b5bad49cce39b66",
    "deacfee92e1b9f001a78d492538d14cfe10d8e5440f28774e89998886b9de329",
}
WORLD_MARKERS = 4
MARKER_BYTES = 2048
SAMPLES = 5
TOP_MAPS = 2
CARD = struct.Struct("<32sIIBBHIIH")
CARDS_PER_FILE = 16384


def table(value):
    return dict(value) if isinstance(value, tuple) else {}


def quaternion(values):
    q = [round(v, 4) + 0.0 for v in values]
    if q[3] < 0 or (q[3] == 0 and next((v for v in q[:3] if v), 0) < 0):
        q = [-v + 0.0 for v in q]
    return q


def accessories_of(blueprint):
    found = []
    for slot, value in sorted(table(blueprint.get("4")).items()):
        entry = table(value)
        asset = entry.get("4")
        if not isinstance(asset, str):
            continue
        found.append((int(entry.get("2") or 0), int(entry.get("1") or 0), asset,
                      round(float(entry.get("3") or 0), 4), round(float(entry.get("5") or 1), 4)))
    return found


def turned(name, relative, rotation, scale, cubes):
    if name not in TURNED or not cubes:
        return relative, rotation
    angle, pivot = TURNED[name]
    if pivot is None:
        xs, _, zs = zip(*cubes)
        pivot = ((min(xs) + max(xs)) / 2, (min(zs) + max(zs)) / 2)
    x, y, z, w = rotation
    local = (pivot[0] * scale, 0.0, pivot[1] * scale)
    tx, ty, tz = 2 * (y * local[2] - z * local[1]), 2 * (z * local[0] - x * local[2]), 2 * (x * local[1] - y * local[0])
    cx = relative[0] + local[0] + w * tx + (y * tz - z * ty)
    cz = relative[2] + local[2] + w * tz + (x * ty - y * tx)
    c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    half_c, half_s = math.cos(math.radians(angle) / 2), math.sin(math.radians(angle) / 2)
    dx, dz = relative[0] - cx, relative[2] - cz
    moved = [cx + c * dx + s * dz, relative[1], cz - s * dx + c * dz]
    return moved, (half_c * x + half_s * z, half_c * y + half_s * w, half_c * z - half_s * x, half_c * w - half_s * y)


def avatar_blob(parts, accessories):
    origin = parts["Torso"][0] if "Torso" in parts else (0.0, 0.0, 0.0)
    out = bytearray(MAGIC)
    out.append(len(PARTS))
    for name in PARTS:
        if name not in parts:
            out.append(0)
            continue
        position, rotation, scale, cubes = parts[name]
        relative, rotation = turned(name, [p - o for p, o in zip(position, origin)], rotation, scale, cubes)
        relative = [round(v * 1024) / 1024 for v in relative]
        data = canonical(cubes)
        out.append(1)
        out += struct.pack(">8f", *relative, *quaternion(rotation), scale) + struct.pack(">I", len(data)) + data
    out.append(len(accessories))
    for slot, item, asset, offset, size in accessories:
        name = asset.encode()
        out += struct.pack(">Biff", slot, item, offset, size) + struct.pack(">H", len(name)) + name
    return bytes(out)


def avatars_in(batches):
    prototypes, objects = parse(batches)
    children = defaultdict(list)
    for wid, (parent, _, _, _) in objects.items():
        children[parent].append(wid)

    inside_class = set()
    for wid, (_, kind, _, _) in objects.items():
        if kind == CLASS:
            stack = list(children[wid])
            while stack:
                child = stack.pop()
                if child not in inside_class:
                    inside_class.add(child)
                    stack.extend(children[child])

    def body(blueprint):
        if blueprint not in objects or objects[blueprint][1] != BLUEPRINT:
            return None
        data = table(table(objects[blueprint][3]).get("BlueprintData"))
        mapping = table(data.get("ChildrenMap"))
        parts = {}
        for name in PARTS:
            wid = mapping.get(name)
            if wid not in objects:
                continue
            _, kind, transform, values = objects[wid]
            prototype = prototypes.get(table(values).get("protoTypeID"))
            if kind != CUBE_MODEL or prototype is None:
                continue
            scale, raw = prototype
            parts[name] = (transform[:3], transform[3:7], round(scale, 4), cubes_of(raw))
        return (parts, accessories_of(data)) if parts else None

    found = {}
    for blueprint, (parent, kind, _, _) in objects.items():
        if kind != BLUEPRINT:
            continue
        holder = objects[parent][1] if parent in objects else None
        if holder == AVATAR and parent in inside_class:
            continue
        source = {CLASS: "class", BUILD_AVATAR: "build", AVATAR: "map"}.get(holder, "placed")
        result = body(blueprint)
        if result is None:
            continue
        parts, accessories = result
        blob = avatar_blob(parts, accessories)
        sha = hashlib.sha256(blob).hexdigest()
        entry = found.setdefault(sha, {
            "blob": blob,
            "cubes": sum(len(part[3]) for part in parts.values()),
            "parts": len(parts),
            "accessories": [asset for _, _, asset, _, _ in accessories],
            "sources": 0,
        })
        entry["sources"] |= SOURCES[source]

    markers = 0
    for scale, raw in prototypes.values():
        if len(raw) <= MARKER_BYTES and hashlib.sha256(model_blob(scale, cubes_of(raw))).hexdigest() in MARKERS:
            markers += 1
    world = markers >= WORLD_MARKERS
    if world:
        for entry in found.values():
            entry["sources"] |= SOURCES["world"]
    return found, world


def read_game(game):
    try:
        found, world = avatars_in(batches_of(get(game["urls"][0]), game["format"]))
        return found, world, None
    except Exception as e:
        return {}, False, str(e)


def scan(shard, shards, limit):
    games = unique_games()
    mine = list(range(shard, len(games), shards))
    if limit:
        mine = mine[:limit]
    print(f"shard {shard}/{shards}: {len(mine)} of {len(games)}", flush=True)

    folder = f"out/avatar-blobs-{shard}"
    os.makedirs(folder, exist_ok=True)
    buckets = [open(os.path.join(folder, f"{bucket:x}.bin"), "wb") for bucket in range(BUCKETS)]
    avatars, worlds, errors = {}, [], 0
    with ThreadPoolExecutor(8) as pool:
        results = pool.map(lambda index: (index, *read_game(games[index])), mine)
        for done, (index, found, world, error) in enumerate(results, 1):
            errors += error is not None
            if world:
                worlds.append(index)
            for sha, avatar in found.items():
                merged = avatars.get(sha)
                if merged is None:
                    packed = zlib.compress(avatar["blob"], 9)
                    buckets[int(sha[0], 16)].write(bytes.fromhex(sha) + struct.pack(">I", len(packed)) + packed)
                    merged = avatars[sha] = {k: v for k, v in avatar.items() if k not in ("blob", "sources")} | {"games": [], "sources": 0}
                merged["games"].append(index)
                merged["sources"] |= avatar["sources"]
            if done % 200 == 0:
                print(f"{done}/{len(mine)} maps, {len(avatars)} avatars, {len(worlds)} avatar worlds", flush=True)
    for bucket in buckets:
        bucket.close()

    with open(f"out/avatars-{shard}.json", "w", encoding="utf-8") as f:
        json.dump({"maps": len(mine), "errors": errors, "worlds": worlds, "avatars": avatars}, f, separators=(",", ":"))
    print(f"shard {shard}: {len(avatars)} avatars, {len(worlds)} avatar worlds, {errors} errors")


def merged(paths):
    avatars, maps, errors, worlds = {}, 0, 0, set()
    for path in paths:
        with open(path, encoding="utf-8") as f:
            shard = json.load(f)
        maps += shard["maps"]
        errors += shard["errors"]
        worlds.update(shard["worlds"])
        for sha, avatar in shard["avatars"].items():
            entry = avatars.get(sha)
            if entry is None:
                entry = avatars[sha] = {**avatar, "games": [], "sources": 0}
            entry["games"].extend(avatar["games"])
            entry["sources"] |= avatar["sources"]
    return avatars, maps, errors, worlds


def source_words(sources):
    return [f"zzsource{name}" for name, bit in SOURCES.items() if sources & bit]


def search_database(words):
    if os.path.exists("out/avatar-search.db"):
        os.remove("out/avatar-search.db")
    db = sqlite3.connect("out/avatar-search.db")
    db.execute(f"PRAGMA page_size = {PAGE_SIZE}")
    db.execute("PRAGMA journal_mode = DELETE")
    db.execute("CREATE VIRTUAL TABLE avatar_search USING fts5(words, content='', detail='none')")
    db.executemany("INSERT INTO avatar_search(rowid, words) VALUES (?, ?)", words)
    db.execute("INSERT INTO avatar_search(avatar_search) VALUES ('optimize')")
    db.commit()
    db.execute("VACUUM")
    db.close()


def index(paths, build):
    games = unique_games()
    avatars, maps, errors, worlds = merged(paths)
    with open("out/locations.tsv", encoding="utf-8") as f:
        locations = {sha: (int(number), int(offset), int(length)) for sha, number, offset, length in (line.split("\t") for line in f)}

    names = {}

    def name(game):
        if game not in names:
            names[game] = repaired(games[game].get("name") or "")
        return names[game]

    order = sorted((sha for sha in avatars if sha in locations), key=lambda sha: len(set(avatars[sha]["games"])), reverse=True)
    top, titled_games, words = [], set(), []
    cards = bytearray()
    for rank, sha in enumerate(order, 1):
        avatar = avatars[sha]
        samples = list(dict.fromkeys(avatar["games"]))
        map_count = len(samples)
        number, offset, length = locations[sha]
        image = map_count >= TOP_MAPS
        titled = next((game for game in samples if name(game)), samples[0] if samples else None)
        if titled is not None:
            titled_games.add(titled)
        cards += CARD.pack(bytes.fromhex(sha), map_count, avatar["cubes"], avatar["sources"], int(image), number, offset, length,
                           0 if titled is None else titled + 1)
        accessories = [os.path.splitext(os.path.basename(asset))[0] for asset in avatar["accessories"]]
        words.append((rank, " ".join([name(game) for game in samples[:SAMPLES]] + accessories + source_words(avatar["sources"]))))
        if image:
            top.append({"sha256": sha, "pack": number, "offset": offset, "length": length})

    search_database(words)
    shutil.rmtree("out/avatar-cards", ignore_errors=True)
    os.makedirs("out/avatar-cards")
    chunk = CARDS_PER_FILE * CARD.size
    for number, start in enumerate(range(0, len(cards), chunk)):
        with open(f"out/avatar-cards/{number:03x}.bin", "wb") as f:
            f.write(cards[start : start + chunk])
    titles = [""] * (max(titled_games, default=-1) + 2)
    for game in titled_games:
        titles[game + 1] = name(game)
    with open("out/avatar-maps.json", "w", encoding="utf-8") as f:
        json.dump(titles, f, ensure_ascii=False, separators=(",", ":"))

    s3 = client()
    base = build_prefix(build, PREFIX)
    upload_file(s3, base + "search.bin", "out/avatar-search.db", "application/octet-stream", IMMUTABLE)
    for card_file in sorted(os.listdir("out/avatar-cards")):
        upload_file(s3, base + "cards/" + card_file, os.path.join("out/avatar-cards", card_file), "application/octet-stream", IMMUTABLE)
    upload_file(s3, base + "maps.json", "out/avatar-maps.json", "application/json; charset=utf-8", IMMUTABLE)
    materials = {}
    if os.path.exists("out/materials/materials.json"):
        with open("out/materials/materials.json", encoding="utf-8") as f:
            materials = json.load(f)
        upload_file(s3, base + "atlas.png", "out/materials/atlas.png", "image/png", IMMUTABLE)

    counts = {source: sum(1 for sha in order if avatars[sha]["sources"] & bit) for source, bit in SOURCES.items()}
    latest = {
        "format": FORMAT,
        "kind": "avatars",
        "build": build,
        "maps": maps,
        "errors": errors,
        "worlds": len(worlds),
        "avatars": len(order),
        "sources": counts,
        "images": len(top),
        "packs": PACKS,
        "search": {"path": "search.bin", "table": "avatar_search", "bytes": os.path.getsize("out/avatar-search.db"), "pageSize": PAGE_SIZE},
        "cards": {"path": "cards/", "record": CARD.size, "perFile": CARDS_PER_FILE},
        "titles": "maps.json",
        "materials": {key: value for key, value in materials.items() if key != "colors"},
    }
    with open("out/latest.json", "w", encoding="utf-8") as f:
        json.dump(latest, f)
    with open("out/top.json", "w", encoding="utf-8") as f:
        json.dump({"build": build, "prefix": PREFIX, "models": top}, f)
    print(f"{maps} maps, {errors} errors, {len(worlds)} avatar worlds, {len(order)} avatars {counts}, {len(top)} with images")


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
        pack(args.pack, args.build, PREFIX)
    elif args.index is not None:
        index(args.index, args.build)
    elif args.publish:
        publish(PREFIX)
    else:
        scan(args.shard, args.shards, args.limit)
