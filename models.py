import argparse
import hashlib
import json
import os
import struct
import sys
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
PREFIX = "models/"
IMMUTABLE = "public, max-age=31536000, immutable"


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
        if not cubes:
            continue
        blob = canonical(cubes)
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

    models, errors = {}, 0
    with ThreadPoolExecutor(8) as pool:
        results = pool.map(lambda index: (index, *read_game(games[index])), mine)
        for done, (index, found, error) in enumerate(results, 1):
            errors += error is not None
            for sha, model in found.items():
                merged = models.get(sha)
                if merged is None:
                    merged = models[sha] = {k: v for k, v in model.items() if k != "blob"} | {"games": [], "authors": Counter(), "items": Counter(), "instances": 0}
                merged["games"].append(index)
                merged["authors"].update(model["authors"])
                merged["items"].update(model["items"])
                merged["instances"] += model["instances"]
            if done % 200 == 0:
                print(f"{done}/{len(mine)} maps, {len(models)} models", flush=True)

    os.makedirs("out", exist_ok=True)
    with open(f"out/models-{shard}.json", "w", encoding="utf-8") as f:
        json.dump({"maps": len(mine), "errors": errors, "models": models}, f, separators=(",", ":"))
    print(f"shard {shard}: {len(models)} models, {errors} errors")


def merge(paths):
    games = unique_games()
    models, maps, errors = {}, 0, 0
    for path in paths:
        shard = json.load(open(path, encoding="utf-8"))
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

    shapes = defaultdict(set)
    for sha, m in models.items():
        for item in m["items"]:
            shapes[item].add(sha)
    products = {item for item, shas in shapes.items() if len(shas) <= PRODUCT_SHAPES}

    catalog, wanted = [], {}
    for sha, m in models.items():
        map_count = len(set(m["games"]))
        items = [i for i, _ in m["items"].most_common() if i in products]
        if map_count < 2:
            continue
        wanted[sha] = m["games"][0]
        catalog.append({
            "sha256": sha,
            "maps": map_count,
            "instances": m["instances"],
            "cubes": m["cubes"],
            "size": m["size"],
            "scale": m["scale"],
            "deformed": m["deformed"],
            "materials": m["materials"],
            "items": items[:5],
            "authors": [a for a, _ in m["authors"].most_common(5)],
            "samples": [{"id": games[g].get("id"), "site": games[g].get("site"), "name": games[g].get("name")} for g in m["games"][:SAMPLES]],
        })
    catalog.sort(key=lambda m: (m["maps"], m["instances"]), reverse=True)

    with open("models.json", "w", encoding="utf-8") as f:
        json.dump({"maps": maps, "errors": errors, "scanned": len(models), "models": catalog}, f, ensure_ascii=False)
    with open("out/wanted.json", "w", encoding="utf-8") as f:
        json.dump(wanted, f)

    shared = sum(1 for m in catalog if m["maps"] > 1)
    priced = sum(1 for m in catalog if m["items"])
    print(f"{maps} maps, {errors} errors, {len(models)} models seen, {len(catalog)} kept: {shared} in more than one map, {priced} with a product item")
    for m in catalog[:15]:
        print(m["maps"], m["instances"], m["cubes"], m["size"], m["items"][:2], m["sha256"][:12], m["samples"][0]["name"] if m["samples"] else "")


def upload(s3, key, data, content_type, cache):
    if s3:
        s3.put_object(Bucket=BUCKET, Key=key, Body=data, ContentType=content_type, CacheControl=cache)


def stored(s3, key):
    try:
        s3.head_object(Bucket=BUCKET, Key=key)
        return True
    except Exception:
        return False


def extract(target):
    s3 = client()
    games = unique_games()
    wanted = json.load(open("out/wanted.json", encoding="utf-8"))
    os.makedirs(target, exist_ok=True)
    missing = {sha: game for sha, game in wanted.items()
               if not os.path.exists(os.path.join(target, sha + ".bin")) and not (s3 and stored(s3, PREFIX + sha + ".bin"))}
    by_game = defaultdict(set)
    for sha, game in missing.items():
        by_game[game].add(sha)
    print(f"{len(missing)} models from {len(by_game)} maps", flush=True)

    def pull(game):
        found, error = read_game(games[game])
        written = 0
        for sha in by_game[game]:
            if sha in found:
                with open(os.path.join(target, sha + ".bin"), "wb") as f:
                    f.write(found[sha]["blob"])
                upload(s3, PREFIX + sha + ".bin", found[sha]["blob"], "application/octet-stream", IMMUTABLE)
                written += 1
        return written, error

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(pull, by_game))
    print(f"{sum(w for w, _ in results)} written, {sum(1 for _, e in results if e)} map errors")
    with open("models.json", "rb") as f:
        upload(s3, PREFIX + "index.json", f.read(), "application/json", "public, max-age=3600")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser()
    p.add_argument("--shard", type=int, default=0)
    p.add_argument("--shards", type=int, default=1)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--merge", nargs="*")
    p.add_argument("--extract", default="")
    args = p.parse_args()
    if args.merge is not None:
        merge(args.merge)
    elif args.extract:
        extract(args.extract)
    else:
        scan(args.shard, args.shards, args.limit)
