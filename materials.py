import argparse
import json
import os

import UnityPy
from PIL import Image

COUNT = 69
COLUMNS_BASE = 16
MARGIN = 0.115384616
FALLBACK = 24
EMISSION = {26: 0.75, 28: 0.7, 45: 1.0, 46: 1.0, 47: 0.5, 55: 0.9, 64: 0.4}
ANIMATED = [26, 27, 28, 51, 54, 63, 64, 67]


def atlas_image(bundle):
    env = UnityPy.load(bundle)
    textures = [obj.read() for obj in env.objects if obj.type.name == "Texture2D"]
    if len(textures) != 1:
        raise ValueError(f"expected one texture in {bundle}, found {len(textures)}")
    return textures[0].image.convert("RGBA")


def layout(width):
    tile = width // COLUMNS_BASE
    margin = int(tile * MARGIN)
    cell = tile + 2 * margin
    return tile, margin, cell, width // cell


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--atlas", required=True, help="Atlas/atlasmid.unity3d or Atlas/atlashigh.unity3d from the newest asset set")
    p.add_argument("--out", default="out/materials")
    p.add_argument("--tile-size", type=int, default=0, help="resize every tile to this many pixels")
    args = p.parse_args()

    atlas = atlas_image(args.atlas)
    width, height = atlas.size
    tile, margin, cell, columns = layout(width)
    os.makedirs(args.out, exist_ok=True)
    atlas.convert("RGB").save(os.path.join(args.out, "atlas.png"), optimize=True)

    colors = {}
    for material in range(COUNT):
        left = material % columns * cell + margin
        top = material // columns * cell + margin
        image = atlas.crop((left, top, left + tile, top + tile))
        if args.tile_size and args.tile_size != tile:
            image = image.resize((args.tile_size, args.tile_size), Image.LANCZOS)
        image.save(os.path.join(args.out, f"{material}.png"))
        pixels = list(image.convert("RGB").get_flattened_data())
        colors[material] = [round(sum(p[i] for p in pixels) / len(pixels)) for i in range(3)]

    info = {
        "atlas": os.path.basename(args.atlas),
        "size": [width, height],
        "tile": tile,
        "margin": margin,
        "columns": columns,
        "count": COUNT,
        "fallback": FALLBACK,
        "emission": EMISSION,
        "animated": ANIMATED,
        "colors": colors,
    }
    with open(os.path.join(args.out, "materials.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, indent=1)
    print(f"{COUNT} materials, {tile}px tiles with {margin}px margins in {columns} columns of a {width}x{height} atlas")


if __name__ == "__main__":
    main()
