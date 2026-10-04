import argparse
import math
import os
import struct
from concurrent.futures import ProcessPoolExecutor

import numpy as np
from PIL import Image

from models import IDENTITY

SIZE = 256
SUPERSAMPLE = 2
MARGIN = 0.08
YAW = math.radians(45)
PITCH = math.radians(30)
LIGHT = np.array([-0.4, 0.8, -0.45])
AMBIENT = 0.55
MAX_TRIANGLES = 200_000

FACES = [
    ([0, 1, 2, 3], (0, 1, 0), 1, 2),
    ([4, 5, 6, 7], (0, -1, 0), 2, 1),
    ([7, 6, 1, 0], (0, 0, -1), 4, 8),
    ([5, 4, 3, 2], (0, 0, 1), 8, 4),
    ([4, 7, 0, 3], (-1, 0, 0), 16, 32),
    ([6, 5, 2, 1], (1, 0, 0), 32, 16),
]
FACE_UV = np.array([[0.0, 1.0], [1.0, 1.0], [1.0, 0.0], [0.0, 0.0]])


def corner(value):
    return (-0.5 + value // 25 * 0.25, -0.5 + value // 5 % 5 * 0.25, -0.5 + value % 5 * 0.25)


def read_model(data):
    cubes = {}
    count = struct.unpack(">i", data[:4])[0]
    pos = 4
    for _ in range(count):
        x, y, z = struct.unpack(">hhh", data[pos : pos + 6])
        flags = data[pos + 6]
        pos += 7
        if flags & 1:
            corners = IDENTITY
        else:
            corners = data[pos : pos + 8]
            pos += 8
        if flags & 2:
            materials = bytes([data[pos]]) * 6
            pos += 1
        else:
            materials = data[pos : pos + 6]
            pos += 6
        for step in range(max(flags >> 2, 1)):
            cubes[(x + step, y, z)] = (bytes(corners), bytes(materials))
    return cubes


def unindented(corners):
    moved = [corners[i] != IDENTITY[i] for i in range(8)]
    count = sum(moved)
    if count == 0:
        return 63
    if count > 4:
        return 0
    sides = 0
    for flag, indices in ((1, (0, 1, 2, 3)), (2, (4, 5, 6, 7)), (8, (2, 3, 4, 5)), (4, (0, 1, 6, 7)), (16, (0, 3, 4, 7)), (32, (1, 2, 5, 6))):
        if not any(moved[i] for i in indices):
            sides |= flag
    return sides


def triangles(cubes):
    flags = {pos: unindented(corners) for pos, (corners, _) in cubes.items()}
    points, uvs, materials = [], [], []
    for (x, y, z), (corners, faces) in cubes.items():
        verts = [tuple(a + b for a, b in zip(corner(c), (x, y, z))) for c in corners]
        for face, (indices, (dx, dy, dz), own, opposite) in enumerate(FACES):
            neighbor = (x + dx, y + dy, z + dz)
            if neighbor in cubes and flags[(x, y, z)] & own and flags[neighbor] & opposite:
                continue
            quad = [verts[i] for i in indices]
            for a, b, c in ((0, 3, 2), (2, 1, 0)):
                points.append((quad[a], quad[b], quad[c]))
                uvs.append((FACE_UV[a], FACE_UV[b], FACE_UV[c]))
                materials.append(faces[face])
    return np.array(points, dtype=np.float64), np.array(uvs, dtype=np.float64), np.array(materials, dtype=np.int32)


def view_matrix():
    cy, sy = math.cos(YAW), math.sin(YAW)
    cp, sp = math.cos(PITCH), math.sin(PITCH)
    yaw = np.array([[cy, 0, -sy], [0, 1, 0], [sy, 0, cy]])
    pitch = np.array([[1, 0, 0], [0, cp, sp], [0, -sp, cp]])
    return pitch @ yaw


def render(data, tiles, size=SIZE):
    cubes = read_model(data)
    tris, uvs, mats = triangles(cubes)
    if len(tris) == 0 or len(tris) > MAX_TRIANGLES:
        return None

    view = view_matrix()
    world_normals = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    lengths = np.linalg.norm(world_normals, axis=1)
    keep = lengths > 1e-9
    tris, uvs, mats, world_normals, lengths = tris[keep], uvs[keep], mats[keep], world_normals[keep], lengths[keep]
    world_normals /= lengths[:, None]
    projected = tris @ view.T
    facing = (world_normals @ view.T)[:, 2] < 0
    tris, uvs, mats, world_normals, projected = tris[facing], uvs[facing], mats[facing], world_normals[facing], projected[facing]
    if len(projected) == 0:
        return None

    light = LIGHT / np.linalg.norm(LIGHT)
    shade = AMBIENT + (1 - AMBIENT) * np.clip(world_normals @ light, 0, 1)

    full = size * SUPERSAMPLE
    flat = projected.reshape(-1, 3)
    low, high = flat[:, :2].min(axis=0), flat[:, :2].max(axis=0)
    scale = full * (1 - 2 * MARGIN) / max(high[0] - low[0], high[1] - low[1], 1e-6)
    center = (low + high) / 2
    screen = np.empty_like(projected)
    screen[..., 0] = (projected[..., 0] - center[0]) * scale + full / 2
    screen[..., 1] = full / 2 - (projected[..., 1] - center[1]) * scale
    screen[..., 2] = projected[..., 2]

    color = np.zeros((full, full, 4), dtype=np.float32)
    depth = np.full((full, full), np.inf, dtype=np.float64)
    for (a, b, c), (ua, ub, uc), material, light_amount in zip(screen, uvs, mats, shade):
        x0 = max(int(math.floor(min(a[0], b[0], c[0]))), 0)
        x1 = min(int(math.ceil(max(a[0], b[0], c[0]))), full - 1)
        y0 = max(int(math.floor(min(a[1], b[1], c[1]))), 0)
        y1 = min(int(math.ceil(max(a[1], b[1], c[1]))), full - 1)
        if x1 < x0 or y1 < y0:
            continue
        area = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        if abs(area) < 1e-12:
            continue
        xs, ys = np.meshgrid(np.arange(x0, x1 + 1) + 0.5, np.arange(y0, y1 + 1) + 0.5)
        w0 = ((b[0] - xs) * (c[1] - ys) - (b[1] - ys) * (c[0] - xs)) / area
        w1 = ((c[0] - xs) * (a[1] - ys) - (c[1] - ys) * (a[0] - xs)) / area
        w2 = 1 - w0 - w1
        inside = (w0 >= -1e-6) & (w1 >= -1e-6) & (w2 >= -1e-6)
        if not inside.any():
            continue
        z = w0 * a[2] + w1 * b[2] + w2 * c[2]
        region = depth[y0 : y1 + 1, x0 : x1 + 1]
        closer = inside & (z < region)
        if not closer.any():
            continue
        region[closer] = z[closer]
        tile = tiles[material if material < len(tiles) else 0]
        th, tw = tile.shape[:2]
        u = (w0 * ua[0] + w1 * ub[0] + w2 * uc[0])[closer]
        v = (w0 * ua[1] + w1 * ub[1] + w2 * uc[1])[closer]
        texels = tile[np.clip((v * th).astype(int), 0, th - 1), np.clip((u * tw).astype(int), 0, tw - 1)]
        pixels = color[y0 : y1 + 1, x0 : x1 + 1]
        pixels[closer, :3] = texels[:, :3] * light_amount
        pixels[closer, 3] = 255

    image = Image.fromarray(np.clip(color, 0, 255).astype(np.uint8), "RGBA")
    return image.resize((size, size), Image.LANCZOS)


def load_tiles(folder):
    tiles = []
    index = 0
    while os.path.exists(os.path.join(folder, f"{index}.png")):
        tiles.append(np.asarray(Image.open(os.path.join(folder, f"{index}.png")).convert("RGBA"), dtype=np.float32))
        index += 1
    return tiles


_tiles = None


def render_file(job):
    global _tiles
    source, target, materials = job
    if _tiles is None:
        _tiles = load_tiles(materials)
    try:
        with open(source, "rb") as f:
            image = render(f.read(), _tiles)
        if image is None:
            return "skipped"
        image.save(target, optimize=True)
        return "rendered"
    except Exception as e:
        return f"error {e}"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--models", default="out/models")
    p.add_argument("--materials", default="out/materials")
    p.add_argument("--out", default="out/images")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--workers", type=int, default=os.cpu_count() or 4)
    args = p.parse_args()

    os.makedirs(args.out, exist_ok=True)
    names = sorted(n for n in os.listdir(args.models) if n.endswith(".bin"))
    jobs = [(os.path.join(args.models, n), os.path.join(args.out, n[:-4] + ".png"), args.materials)
            for n in names if not os.path.exists(os.path.join(args.out, n[:-4] + ".png"))]
    if args.limit:
        jobs = jobs[: args.limit]
    results = {}
    with ProcessPoolExecutor(args.workers) as pool:
        for result in pool.map(render_file, jobs, chunksize=4):
            key = result.split(" ")[0]
            results[key] = results.get(key, 0) + 1
    print(f"{len(jobs)} models: {results}")


if __name__ == "__main__":
    main()
