import argparse
import gzip
import hashlib
import json
import os
import struct
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

INDEX = "https://cdn.openkogama.org/games.json"
SUPPLEMENT = 0x4B474D53
AVATARS = {0, 133}


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "games-scan"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return r.read()
        except Exception as e:
            err = e
    raise RuntimeError(err)


class Reader:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def take(self, n):
        value = self.data[self.pos : self.pos + n]
        if len(value) != n:
            raise ValueError("truncated")
        self.pos += n
        return value

    def i32(self):
        return struct.unpack(">i", self.take(4))[0]

    def f32(self):
        return struct.unpack(">f", self.take(4))[0]

    def u8(self):
        return self.take(1)[0]

    def text(self):
        size = shift = 0
        while True:
            b = self.u8()
            size |= (b & 0x7F) << shift
            shift += 7
            if b < 0x80:
                return self.take(size).decode("utf-8", "replace")

    def value(self, kind):
        if kind == 0:
            return self.i32()
        if kind in (1, 4):
            return tuple(self.i32() for _ in range(self.i32()))
        if kind == 2:
            return self.f32()
        if kind == 3:
            return tuple(self.f32() for _ in range(self.i32()))
        if kind == 5:
            return self.u8() != 0
        if kind == 6:
            return self.take(self.i32())
        if kind == 7:
            return self.text()
        if kind == 8:
            return self.table()
        if kind == 9:
            return self.u8()
        if kind == 10:
            return struct.unpack(">Q", self.take(8))[0]
        if kind == 11:
            return tuple(struct.unpack(">Q", self.take(8))[0] for _ in range(self.i32()))
        raise ValueError(f"type {kind}")

    def table(self):
        pairs = []
        for _ in range(self.i32()):
            key = self.text()
            pairs.append((key, self.value(self.u8())))
        return tuple(pairs)


def batches_of(data, fmt):
    if fmt == "kgm":
        return [data]
    raw = gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
    if raw[:4] != b"KGMP":
        raise ValueError("not a kgmap")
    version = struct.unpack("<H", raw[4:6])[0]
    offset = 6
    if version >= 6:
        offset += 4 + struct.unpack("<i", raw[6:10])[0]
    batches = []
    for _ in range(struct.unpack("<i", raw[offset : offset + 4])[0]):
        size = struct.unpack("<i", raw[offset + 4 : offset + 8])[0]
        batches.append(raw[offset + 8 : offset + 8 + size])
        offset += 4 + size
    return batches


def parse(batches):
    prototypes, objects = {}, {}
    for batch in batches:
        r = Reader(batch)
        if len(batch) >= 4 and struct.unpack(">i", batch[:4])[0] == SUPPLEMENT:
            r.i32()
            for _ in range(r.i32()):
                pid, scale, _, size = r.i32(), r.f32(), r.i32(), r.i32()
                prototypes[pid] = (scale, r.take(size))
            continue
        for _ in range(r.i32()):
            pid, scale, _, size = r.i32(), r.f32(), r.i32(), r.i32()
            prototypes[pid] = (scale, r.take(size))
        for _ in range(r.i32()):
            wid, parent, _, kind = r.i32(), r.i32(), r.i32(), r.i32()
            transform = struct.unpack(">10f", r.take(40))
            data = r.table()
            flags = r.u8()
            if flags & 1:
                r.i32()
            if flags & 2:
                r.i32()
            r.table()
            objects[wid] = (parent, kind, transform, data)
    return prototypes, objects


def normalize(value):
    if isinstance(value, float):
        return round(value, 3)
    if isinstance(value, bytes):
        return hashlib.sha256(value).hexdigest()
    if isinstance(value, tuple):
        return tuple(normalize(v) for v in value)
    return value


def fingerprint(batches):
    prototypes, objects = parse(batches)

    children = defaultdict(list)
    for wid, (parent, _, _, _) in objects.items():
        children[parent].append(wid)
    skipped, stack = set(), [wid for wid, obj in objects.items() if obj[1] in AVATARS]
    while stack:
        wid = stack.pop()
        if wid not in skipped:
            skipped.add(wid)
            stack.extend(children[wid])

    entries = []
    for wid, (_, kind, transform, data) in objects.items():
        if wid in skipped:
            continue
        values = dict(data)
        prototype = prototypes.get(values.pop("protoTypeID", None))
        shape = (round(prototype[0], 4), hashlib.sha256(prototype[1]).hexdigest()) if prototype else None
        entries.append(repr((kind, normalize(transform), normalize(tuple(sorted(values.items()))), shape)))

    entries.sort()
    return hashlib.sha256("\n".join(entries).encode()).hexdigest(), len(entries)


def describe(game):
    try:
        world, count = fingerprint(batches_of(get(game["urls"][0]), game["format"]))
        return {"sha256": game["sha256"], "worldHash": world, "objects": count}
    except Exception as e:
        return {"sha256": game["sha256"], "error": str(e)}


def scan(shard, shards):
    games = [g for g in json.loads(get(INDEX))["games"] if g.get("urls") and g.get("sha256")]
    unique = sorted({g["sha256"]: g for g in games}.values(), key=lambda g: g["sha256"])
    mine = unique[shard::shards]
    print(f"shard {shard}/{shards}: {len(mine)} of {len(unique)}", flush=True)
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(describe, mine))
    os.makedirs("out", exist_ok=True)
    with open(f"out/datahash-{shard}.json", "w", encoding="utf-8") as f:
        json.dump(results, f)


def merge(paths):
    games = json.loads(get(INDEX))["games"]
    hashes, errors = {}, 0
    for path in paths:
        for r in json.load(open(path, encoding="utf-8")):
            if "worldHash" in r:
                hashes[r["sha256"]] = r
            else:
                errors += 1

    groups = defaultdict(list)
    for g in games:
        if g.get("sha256") in hashes:
            groups[hashes[g["sha256"]]["worldHash"]].append(g)

    ranking = [
        {
            "worldHash": world,
            "count": len(members),
            "objects": hashes[members[0]["sha256"]]["objects"],
            "games": [
                {"id": g.get("id"), "site": g.get("site"), "name": g.get("name"), "authorId": g.get("authorId"), "sha256": g["sha256"]}
                for g in members
            ],
        }
        for world, members in groups.items()
        if len(members) > 1
    ]
    ranking.sort(key=lambda r: r["count"], reverse=True)

    with open("datahash.json", "w", encoding="utf-8") as f:
        json.dump({"scanned": len(hashes), "errors": errors, "groups": ranking}, f, indent=2, ensure_ascii=False)
    print(f"{len(hashes)} maps, {errors} errors, {len(ranking)} repeated worlds")
    for r in ranking[:20]:
        print(r["count"], r["objects"], r["worldHash"][:12], r["games"][0]["name"])


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--shard", type=int, default=0)
    p.add_argument("--shards", type=int, default=1)
    p.add_argument("--merge", nargs="*")
    args = p.parse_args()
    if args.merge is not None:
        merge(args.merge)
    else:
        scan(args.shard, args.shards)
