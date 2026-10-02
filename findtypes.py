import argparse
import base64
import json
import os
import struct
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from datahash import INDEX, SUPPLEMENT, Reader, batches_of, get

LIMIT = 20


class TypedReader(Reader):
    def typed_value(self, kind):
        if kind == 6:
            return base64.b64encode(self.take(self.i32())).decode()
        if kind == 8:
            return self.typed_table()
        value = self.value(kind)
        return list(value) if isinstance(value, tuple) else value

    def typed_table(self):
        pairs = []
        for _ in range(self.i32()):
            key = self.text()
            kind = self.u8()
            pairs.append([key, kind, self.typed_value(kind)])
        return pairs


def parse(batches):
    prototypes, objects = {}, {}
    for batch in batches:
        r = TypedReader(batch)
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
            transform = list(struct.unpack(">10f", r.take(40)))
            data = r.typed_table()
            flags = r.u8()
            if flags & 1:
                r.i32()
            if flags & 2:
                r.i32()
            r.typed_table()
            objects[wid] = {"id": wid, "parent": parent, "item": item, "type": kind, "transform": transform, "data": data}
    return prototypes, objects


def prototype_ids(data):
    for key, kind, value in data:
        if key == "protoTypeID" and kind == 0:
            yield value
        elif kind == 8:
            yield from prototype_ids(value)


def subtree(root, objects, prototypes):
    children = defaultdict(list)
    for obj in objects.values():
        children[obj["parent"]].append(obj["id"])
    found, stack = [], [root]
    while stack:
        wid = stack.pop()
        found.append(objects[wid])
        stack.extend(children[wid])
    used = {pid for obj in found for pid in prototype_ids(obj["data"]) if pid in prototypes}
    return {
        "objects": found,
        "prototypes": [
            {"id": pid, "scale": prototypes[pid][0], "author": prototypes[pid][1], "cubes": base64.b64encode(prototypes[pid][2]).decode()}
            for pid in sorted(used)
        ],
    }


def search(game, types):
    try:
        prototypes, objects = parse(batches_of(get(game["urls"][0]), game["format"]))
    except Exception as e:
        return {"sha256": game["sha256"], "error": str(e)}
    hits = []
    for obj in objects.values():
        if obj["type"] in types:
            hits.append({"type": obj["type"], **subtree(obj["id"], objects, prototypes)})
    info = {key: game.get(key) for key in ("id", "site", "name", "authorId", "sha256")}
    return {**info, "hits": hits}


def scan(shard, shards, types):
    games = [g for g in json.loads(get(INDEX))["games"] if g.get("urls") and g.get("sha256")]
    unique = sorted({g["sha256"]: g for g in games}.values(), key=lambda g: g["sha256"])
    mine = unique[shard::shards]
    print(f"shard {shard}/{shards}: {len(mine)} of {len(unique)}", flush=True)
    with ThreadPoolExecutor(8) as pool:
        results = [r for r in pool.map(lambda g: search(g, types), mine) if r.get("hits") or r.get("error")]
    os.makedirs("out", exist_ok=True)
    with open(f"out/findtypes-{shard}.json", "w", encoding="utf-8") as f:
        json.dump(results, f)


def merge(paths):
    found, errors, maps = defaultdict(list), 0, defaultdict(int)
    for path in paths:
        for r in json.load(open(path, encoding="utf-8")):
            if "error" in r:
                errors += 1
                continue
            for hit in r["hits"]:
                maps[hit["type"]] += 1
                if len(found[hit["type"]]) < LIMIT:
                    found[hit["type"]].append({"game": {k: r[k] for k in ("id", "site", "name", "authorId", "sha256")}, **hit})
    with open("findtypes.json", "w", encoding="utf-8") as f:
        json.dump({"errors": errors, "counts": maps, "found": found}, f, ensure_ascii=False)
    print(f"{errors} errors")
    for kind, count in sorted(maps.items()):
        print(f"type {kind}: {count} objects, kept {len(found[kind])}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--shard", type=int, default=0)
    p.add_argument("--shards", type=int, default=1)
    p.add_argument("--types", default="58,59")
    p.add_argument("--merge", nargs="*")
    args = p.parse_args()
    if args.merge is not None:
        merge(args.merge)
    else:
        scan(args.shard, args.shards, {int(t) for t in args.types.split(",")})
