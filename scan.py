import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import urllib.parse
import urllib.request
import zipfile

META = "https://archive.org/metadata/"
DOWNLOAD = "https://archive.org/download/"
MAPS = (".kgmap", ".kgm")
ARCHIVES = (".zip", ".rar")
VERSIONED = re.compile(r"\.~[0-9]+~$")
NUMERIC = re.compile(r"^[0-9]{3,9}$")
REGIONS = {"WWW.KOGAMA": "www", "KOGAMA.COM.BR": "br", "FRIENDS.KOGAMA": "friends"}
BUCKET = "openkogama"
PREFIX = "games/"


def items():
    return [l.strip() for l in open("items.txt", encoding="utf-8") if l.strip() and not l.startswith("#")]


def get(url, path=None):
    req = urllib.request.Request(url, headers={"User-Agent": "games-scan"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                if path is None:
                    return r.read()
                with open(path, "wb") as f:
                    shutil.copyfileobj(r, f)
                return None
        except Exception as e:
            err = e
            print(f"  retry {attempt + 1}: {e}", flush=True)
    raise RuntimeError(err)


def url_of(item, name):
    return DOWNLOAD + item + "/" + urllib.parse.quote(name)


def listing():
    found = {}
    for item in items():
        meta = json.loads(get(META + item))
        for f in meta.get("files", []):
            name = f["name"]
            plain = VERSIONED.sub("", name).lower()
            if not plain.endswith(MAPS + ARCHIVES):
                continue
            key = f.get("md5") or item + "/" + name
            rec = found.setdefault(key, {"size": int(f.get("size") or 0), "paths": []})
            rec["paths"].append([item, name])
        print(f"{item}: {len(found)}", flush=True)
    return [found[k] for k in sorted(found)]


def region_of(name):
    return REGIONS.get(name.split("/")[0].upper(), "")


def header(data):
    raw = gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
    if raw[:4] != b"KGMP":
        return 0, {}
    version = struct.unpack("<H", raw[4:6])[0]
    if version < 6:
        return version, {}
    size = struct.unpack("<I", raw[6:10])[0]
    try:
        return version, json.loads(raw[10 : 10 + size])
    except Exception:
        return version, {}


def describe(data, name, sources):
    plain = VERSIONED.sub("", os.path.basename(name))
    stem, ext = os.path.splitext(plain)
    if not ext:
        stem, ext = "", "." + plain.rsplit(".", 1)[-1]
    ext = ext.lower()
    version, meta = header(data) if ext == ".kgmap" else (0, {})
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "format": ext.lstrip("."),
        "formatVersion": version,
        "exporter": meta.get("Kgmexporter", ""),
        "savedAt": meta.get("SavedAt", ""),
        "id": str(meta.get("GameId") or (stem if NUMERIC.match(stem) else "")),
        "name": meta.get("GameTitle", "").strip(),
        "fileName": "" if NUMERIC.match(stem) else stem,
        "authorId": meta.get("OwnerProfileId", ""),
        "region": meta.get("Region", "") or region_of(name),
        "sources": sources,
    }


def store(s3, rec, data):
    key = PREFIX + rec["sha256"] + "." + rec["format"]
    if s3:
        try:
            s3.head_object(Bucket=BUCKET, Key=key)
        except Exception:
            s3.put_object(
                Bucket=BUCKET,
                Key=key,
                Body=data,
                ContentType="application/octet-stream",
                CacheControl="public, max-age=31536000, immutable",
            )
    rec["key"] = key


def unpack(path, ext):
    out = tempfile.mkdtemp()
    if ext == ".zip":
        with zipfile.ZipFile(path) as z:
            z.extractall(out)
    else:
        subprocess.run(["unar", "-q", "-D", "-o", out, path], check=True)
    for root, _, names in os.walk(out):
        for n in names:
            if n.lower().endswith(MAPS):
                yield os.path.join(root, n), n
    shutil.rmtree(out, ignore_errors=True)


def client():
    key = os.environ.get("R2_ACCESS_KEY_ID")
    if not key:
        return None
    import boto3

    s3 = boto3.client(
        "s3",
        endpoint_url="https://%s.r2.cloudflarestorage.com" % os.environ["R2_ACCOUNT_ID"],
        aws_access_key_id=key,
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )
    s3.head_bucket(Bucket=BUCKET)
    return s3


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--shard", type=int, default=0)
    p.add_argument("--shards", type=int, default=1)
    p.add_argument("--limit", type=int, default=0)
    args = p.parse_args()

    everything = listing()
    mine = [f for i, f in enumerate(everything) if i % args.shards == args.shard]
    if args.limit:
        mine = mine[: args.limit]
    print(f"shard {args.shard}/{args.shards}: {len(mine)} of {len(everything)}", flush=True)

    s3 = client()
    os.makedirs("out", exist_ok=True)
    results = []
    for i, f in enumerate(mine, 1):
        paths = sorted(f["paths"], key=lambda p: not NUMERIC.match(os.path.splitext(os.path.basename(VERSIONED.sub("", p[1])))[0]))
        item, name = paths[0]
        sources = [url_of(*p) for p in paths]
        print(f"[{i}/{len(mine)}] {item}/{name}", flush=True)
        ext = os.path.splitext(VERSIONED.sub("", name))[1].lower()
        try:
            if ext in ARCHIVES:
                fd, tmp = tempfile.mkstemp(suffix=ext)
                os.close(fd)
                get(sources[0], tmp)
                for path, inner in unpack(tmp, ext):
                    data = open(path, "rb").read()
                    rec = describe(data, inner, [])
                    store(s3, rec, data)
                    results.append(rec)
                os.unlink(tmp)
            else:
                data = get(sources[0])
                rec = describe(data, name, sources)
                store(s3, rec, data)
                results.append(rec)
        except Exception as e:
            print(f"  failed: {e}", flush=True)
            results.append({"error": str(e), "sources": sources})

    with open(f"out/shard-{args.shard}.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=4)


if __name__ == "__main__":
    main()
