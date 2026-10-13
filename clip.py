import argparse, colorsys, io, json, os, struct, urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import open_clip
import torch
from PIL import Image

CDN = "https://cdn.openkogama.org/models/v2/"
TYPES = {
    "building": "a house or building", "furniture": "furniture like a table, chair, bed or shelf",
    "vehicle": "a vehicle like a car, plane, boat or tank", "weapon": "a weapon like a sword or gun",
    "plant": "a tree, plant or flower", "nature": "rocks, terrain, water or clouds", "food": "food or a drink",
    "animal": "an animal", "character": "a person, character or creature", "decoration": "a decoration like a statue, lamp or fence",
    "sign": "a sign with text or a logo", "structure": "a wall, bridge, stairs, tower or platform", "tool": "a tool or device",
    "object": "a small object",
}
FLAGS = {
    "hidden": ["a swastika", "a nazi symbol", "naked human body with genitals", "a penis", "sexual content"],
    "mature": ["blood and gore", "a woman in a bikini", "a middle finger gesture"],
    "safe": ["a colorful toy made of blocks", "a 3d model of an everyday object", "a building made of cubes", "a cartoon character"],
}


def get(url):
    for _ in range(3):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "games-scan"}), timeout=120).read()
        except Exception:
            pass
    return None


def family(r, g, b):
    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255); h *= 360
    if v < 0.15 or (v < 0.25 and s < 0.4): return "black", s, v
    if s < 0.2: return "light", s, v
    if h < 15 or h >= 340: return "red", s, v
    if h < 50: return "warm", s, v
    if h < 70: return "yellow", s, v
    if h < 170: return "green", s, v
    if h < 255: return "blue", s, v
    if h < 295: return "purple", s, v
    return "pink", s, v


def color(image):
    im = image.convert("RGBA"); im.thumbnail((96, 96))
    fam = defaultdict(list)
    for r, g, b, a in im.getdata():
        if a >= 160:
            f, s, v = family(r, g, b); fam[f].append((s, v))
    named = {}
    for f, px in fam.items():
        ss = sorted(p[0] for p in px)[len(px) // 2]; vv = sorted(p[1] for p in px)[min(len(px) - 1, int(0.8 * len(px)))]
        n = {"light": "white" if vv >= 0.8 else "gray", "warm": "orange" if ss >= 0.68 else "brown",
             "red": "pink" if ss < 0.5 and vv > 0.75 else "red"}.get(f, f)
        named[n] = named.get(n, 0) + len(px)
    return max(named, key=named.get) if named else None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--shard", type=int, default=0)
    p.add_argument("--shards", type=int, default=1)
    args = p.parse_args()
    latest = json.loads(get(CDN + "latest.json"))
    base = f"{CDN}builds/{latest['build']}/"
    cards = latest["cards"]; size = cards["record"]
    rows = []
    for part in range((latest["images"] + cards["perFile"] - 1) // cards["perFile"] + 1):
        data = get(f"{base}{cards['path']}{part:03x}.bin") or b""
        for at in range(0, len(data) - size + 1, size):
            if data[at + 44]:
                rows.append((part * cards["perFile"] + at // size + 1, data[at:at + 32].hex()))
    rows = rows[args.shard::args.shards]
    model, _, preprocess = open_clip.create_model_and_transforms("ViT-B-32", pretrained="laion2b_s34b_b79k")
    tokenizer = open_clip.get_tokenizer("ViT-B-32")
    labels = list(TYPES) + [f"{k}:{t}" for k, ts in FLAGS.items() for t in ts]
    prompts = [f"a blocky 3d render of {TYPES[k]}" for k in TYPES] + [f"a blocky 3d render of {t}" for ts in FLAGS.values() for t in ts]
    with torch.no_grad():
        text = model.encode_text(tokenizer(prompts)); text /= text.norm(dim=-1, keepdim=True)
    out = {}
    with ThreadPoolExecutor(8) as pool:
        images = pool.map(lambda r: (r, get(f"{base}images/{r[1]}.png")), rows)
        batch = []
        def flush():
            if not batch: return
            with torch.no_grad():
                feats = model.encode_image(torch.stack([preprocess(im.convert("RGB")) for _, im in batch]))
                feats /= feats.norm(dim=-1, keepdim=True)
                probs = (100 * feats @ text.T).softmax(dim=-1)
            for (row, im), pr in zip(batch, probs):
                t = pr[:len(TYPES)]; kind = list(TYPES)[int(t.argmax())]
                f = pr[len(TYPES):]; groups = defaultdict(float)
                for label, value in zip(labels[len(TYPES):], f.tolist()): groups[label.split(":")[0]] += value
                total = sum(groups.values()) or 1
                hidden, mature = groups["hidden"] / total, groups["mature"] / total
                rating = "hidden" if hidden > 0.6 else "mature" if mature > 0.6 else ""
                out[row[0]] = [row[1], kind, color(im), rating, round(hidden, 3), round(mature, 3)]
            batch.clear()
        for row, data in images:
            if not data: continue
            try: batch.append((row, Image.open(io.BytesIO(data))))
            except Exception: continue
            if len(batch) == 64: flush()
        flush()
    os.makedirs("out", exist_ok=True)
    json.dump(out, open(f"out/clip-{args.shard}.json", "w"))
    print(len(out), "models")


if __name__ == "__main__":
    main()
