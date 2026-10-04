import argparse
import json
import os

import UnityPy

GRID = [
    (0.001953, 0.876953), (0.126953, 0.876953), (0.251953, 0.876953), (0.376953, 0.876953),
    (0.501953, 0.876953), (0.626953, 0.876953), (0.751953, 0.876953), (0.876953, 0.876953),
    (0.001953, 0.751953), (0.126953, 0.751953), (0.251953, 0.751953), (0.376953, 0.751953),
    (0.501953, 0.751953), (0.626953, 0.751953), (0.751953, 0.751953), (0.876953, 0.751953),
    (0.001953, 0.626953), (0.126953, 0.626953), (0.251953, 0.626953), (0.376953, 0.626953),
    (0.501953, 0.626953), (0.626953, 0.626953), (0.751953, 0.626953), (0.876953, 0.626953),
    (0.001953, 0.501953), (0.126953, 0.501953), (0.126953, 0.001953), (0.251953, 0.501953),
    (0.251953, 0.001953), (0.376953, 0.501953), (0.501953, 0.501953), (0.626953, 0.501953),
    (0.751953, 0.501953), (0.876953, 0.501953), (0.001953, 0.376953), (0.126953, 0.376953),
    (0.251953, 0.376953), (0.376953, 0.376953), (0.501953, 0.376953), (0.626953, 0.376953),
    (0.751953, 0.376953), (0.876953, 0.376953), (0.001953, 0.251953), (0.126953, 0.251953),
    (0.251953, 0.251953), (0.376953, 0.251953), (0.501953, 0.251953), (0.626953, 0.251953),
    (0.751953, 0.251953), (0.876953, 0.251953), (0.001953, 0.126953), (0.126953, 0.126953),
    (0.251953, 0.126953), (0.376953, 0.126953), (0.501953, 0.126953), (0.376953, 0.001953),
    (0.626953, 0.126953), (0.751953, 0.126953), (0.876953, 0.126953), (0.001953, 0.001953),
    (0.501953, 0.001953),
]
TILE = 0.121094


def atlas_image(bundle):
    env = UnityPy.load(bundle)
    textures = [obj.read() for obj in env.objects if obj.type.name == "Texture2D"]
    if len(textures) != 1:
        raise ValueError(f"expected one texture in {bundle}, found {len(textures)}")
    return textures[0].image.convert("RGBA")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--atlas", required=True, help="Atlas/atlas512glowing.unity3d from the streaming assets")
    p.add_argument("--out", default="out/materials")
    args = p.parse_args()

    atlas = atlas_image(args.atlas)
    width, height = atlas.size
    os.makedirs(args.out, exist_ok=True)
    palette = {}
    for material, (u, v) in enumerate(GRID):
        left = round(u * width)
        top = round((1 - v - TILE) * height)
        size = round(TILE * width)
        tile = atlas.crop((left, top, left + size, top + size))
        tile.save(os.path.join(args.out, f"{material}.png"))
        pixels = list(tile.get_flattened_data())
        palette[material] = [round(sum(p[i] for p in pixels) / len(pixels)) for i in range(3)]

    with open(os.path.join(args.out, "materials.json"), "w", encoding="utf-8") as f:
        json.dump({"atlas": os.path.basename(args.atlas), "size": [width, height], "colors": palette}, f, indent=1)
    print(f"{len(palette)} materials from a {width}x{height} atlas")


if __name__ == "__main__":
    main()
