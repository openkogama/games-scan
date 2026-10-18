import argparse
import json
import sys

from datahash import get
from models import SHORT, build_prefix, client, store_links, unique_games, upload
from scan import BUCKET

CDN = "https://cdn.openkogama.org/"
KINDS = {"models": ("models/v2/", ""), "avatars": ("avatars/v1/", "avatar-")}


def card_ids(base, latest, kind):
    cards, ids = latest["cards"], {}
    for number in range(-(-latest[kind] // cards["perFile"])):
        data = get(f"{base}{cards['path']}{number:03x}.bin")
        for at in range(0, len(data), cards["record"]):
            item = number * cards["perFile"] + at // cards["record"] + 1
            ids[data[at : at + 32].hex()] = item, int.from_bytes(data[at + 32 : at + 36], "little")
    return ids


def shard_games(paths, kind):
    games = {}
    for path in paths:
        with open(path, encoding="utf-8") as f:
            for sha, entry in json.load(f)[kind].items():
                games.setdefault(sha, set()).update(entry["games"])
    return games


def backfill(kind, build, paths):
    prefix, files = KINDS[kind]
    s3 = client()
    latest = json.loads(s3.get_object(Bucket=BUCKET, Key=prefix + "latest.json")["Body"].read() if s3 else get(CDN + prefix + "latest.json"))
    if latest["build"] != build:
        sys.exit(f"latest {kind} build is {latest['build']}, not {build}")
    base = build_prefix(build, prefix)
    ids = card_ids(CDN + base, latest, kind)
    found = shard_games(paths, kind)
    items = [(item, found.get(sha, ())) for sha, (item, _) in ids.items()]
    wrong = sum(1 for sha, (_, maps) in ids.items() if len(found.get(sha, ())) != maps)
    if wrong:
        sys.exit(f"{wrong} of {len(ids)} {kind} have another map count than the build")
    latest["links"] = store_links(s3, base, items, unique_games(), files)
    upload(s3, prefix + "latest.json", json.dumps(latest).encode(), "application/json", SHORT)
    print(f"{len(ids)} {kind}, links {latest['links']['bytes'] // 1024 // 1024} MB")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser()
    p.add_argument("--kind", choices=KINDS, required=True)
    p.add_argument("--build", required=True)
    p.add_argument("shards", nargs="+")
    args = p.parse_args()
    backfill(args.kind, args.build, args.shards)
