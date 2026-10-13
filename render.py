import argparse
import json
import math
import os
import struct
import urllib.request
import zlib
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

import numpy as np
from PIL import Image

from models import IDENTITY, IMMUTABLE, PREFIX, build_prefix, cubes_of, split_blob, upload
from scan import client

CDN = "https://cdn.openkogama.org/"

SIZE = 256
SUPERSAMPLE = 2
FIELD_OF_VIEW = 35.0
CAMERA_OFFSET = np.array([float(v) for v in os.environ.get("RENDER_CAMERA", "4.57,2.0,0.15").split(",")])
PREVIEW_SIZE = 2.0
TURN = float(os.environ.get("RENDER_TURN", "-45.0"))
LIGHT_EULER = (50.0, -30.0)
LIGHT_COLOR = np.array([1.0, 0.988, 0.753])
AMBIENT = 0.45
OCCLUSION = 0.5
NEAR = 0.05
MAX_TRIANGLES = 200_000
AVATAR_MAGIC = b"OKA1"
AVATAR_TURN = 180.0

FACES = [
    ([0, 1, 2, 3], (0, 1, 0), 1, 2),
    ([4, 5, 6, 7], (0, -1, 0), 2, 1),
    ([7, 6, 1, 0], (0, 0, -1), 4, 8),
    ([5, 4, 3, 2], (0, 0, 1), 8, 4),
    ([4, 7, 0, 3], (-1, 0, 0), 16, 32),
    ([6, 5, 2, 1], (1, 0, 0), 32, 16),
]
FACE_UV = [(0, 1, 2, 0.5), (0, -1, 2, -0.5), (0, 1, 1, 0.5), (0, -1, 1, -0.5), (2, -1, 1, -0.5), (2, 1, 1, 0.5)]


def corner(value):
    return (-0.5 + value // 25 * 0.25, -0.5 + value // 5 % 5 * 0.25, -0.5 + value % 5 * 0.25)


IDENTITY_CORNERS = [corner(c) for c in IDENTITY]


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


def face_uv(face, point, scale):
    axis_u, sign_u, axis_v, offset_u = FACE_UV[face]
    return ((sign_u * point[axis_u] + offset_u) * scale * 0.5, (point[axis_v] + 0.5) * scale * 0.5)


def occlusion(cubes, cube, normal, canonical):
    axis = next(i for i in range(3) if normal[i])
    base = tuple(c + n for c, n in zip(cube, normal))
    tangents = [i for i in range(3) if i != axis]
    steps = []
    for i in tangents:
        step = [0, 0, 0]
        step[i] = 1 if canonical[i] > 0 else -1
        steps.append(step)
    cells = [base, tuple(b + s for b, s in zip(base, steps[0])), tuple(b + s for b, s in zip(base, steps[1])),
             tuple(b + s + t for b, s, t in zip(base, steps[0], steps[1]))]
    return sum(1 for cell in cells if cell not in cubes) * 0.25


def triangles(cubes, scale, count, fallback):
    flags = {pos: unindented(corners) for pos, (corners, _) in cubes.items()}
    points, uvs, lights, materials, normals = [], [], [], [], []
    for cube, (corners, faces) in cubes.items():
        verts = [tuple(a + b for a, b in zip(corner(c), cube)) for c in corners]
        for face, (indices, normal, own, opposite) in enumerate(FACES):
            neighbor = tuple(c + n for c, n in zip(cube, normal))
            if neighbor in cubes and flags[cube] & own and flags[neighbor] & opposite:
                continue
            quad = [verts[i] for i in indices]
            quad_uv = [face_uv(face, p, scale) for p in quad]
            quad_light = [occlusion(cubes, cube, normal, IDENTITY_CORNERS[i]) for i in indices]
            material = faces[face] if faces[face] < count else fallback
            for a, b, c in ((0, 3, 2), (2, 1, 0)):
                points.append((quad[a], quad[b], quad[c]))
                uvs.append((quad_uv[a], quad_uv[b], quad_uv[c]))
                lights.append((quad_light[a], quad_light[b], quad_light[c]))
                materials.append(material)
                normals.append(normal)
    return (np.array(points, dtype=np.float64), np.array(uvs, dtype=np.float64), np.array(lights, dtype=np.float64),
            np.array(materials, dtype=np.int32), np.array(normals, dtype=np.float64))


def quaternion_matrix(x, y, z, w):
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])


def parts_of(data):
    if data[:4] != AVATAR_MAGIC:
        scale, cube_data = split_blob(data)
        return [(cubes_of(cube_data), scale, np.eye(3), np.zeros(3))], 0.0
    pos = 5
    parts = []
    for _ in range(data[4]):
        present = data[pos]
        pos += 1
        if not present:
            continue
        values = struct.unpack(">8f", data[pos : pos + 32])
        size = struct.unpack(">I", data[pos + 32 : pos + 36])[0]
        pos += 36
        parts.append((cubes_of(data[pos : pos + size]), values[7], quaternion_matrix(*values[3:7]), np.array(values[:3])))
        pos += size
    return parts, AVATAR_TURN


def geometry(data, count, fallback):
    parts, turn = parts_of(data)
    pieces = []
    for cubes, scale, rotation, offset in parts:
        tris, uvs, lights, mats, normals = triangles(cubes, scale, count, fallback)
        if len(tris):
            pieces.append((tris * scale @ rotation.T + offset, uvs, lights, mats, normals @ rotation.T))
    if not pieces:
        return None, turn
    return tuple(np.concatenate(column) for column in zip(*pieces)), turn


def yaw(degrees):
    t = math.radians(degrees)
    return np.array([[math.cos(t), 0, math.sin(t)], [0, 1, 0], [-math.sin(t), 0, math.cos(t)]])


def pitch(degrees):
    t = math.radians(degrees)
    return np.array([[1, 0, 0], [0, math.cos(t), -math.sin(t)], [0, math.sin(t), math.cos(t)]])


def light_direction():
    forward = yaw(LIGHT_EULER[1]) @ pitch(LIGHT_EULER[0]) @ np.array([0.0, 0.0, 1.0])
    return -forward / np.linalg.norm(forward)


def sample(tile, u, v):
    size = tile.shape[0]
    x = (u - np.floor(u)) * size - 0.5
    y = (1 - (v - np.floor(v))) * size - 0.5
    x0, y0 = np.floor(x).astype(int), np.floor(y).astype(int)
    fx, fy = (x - x0)[:, None], (y - y0)[:, None]
    x0, y0 = x0 % size, y0 % size
    x1, y1 = (x0 + 1) % size, (y0 + 1) % size
    top = tile[y0, x0] * (1 - fx) + tile[y0, x1] * fx
    bottom = tile[y1, x0] * (1 - fx) + tile[y1, x1] * fx
    return top * (1 - fy) + bottom * fy


def render(data, materials, size=SIZE):
    tiles, emission, fallback = materials
    built, turn = geometry(data, len(tiles), fallback)
    if built is None:
        return None
    tris, uvs, lights, mats, face_normals = built
    if len(tris) > MAX_TRIANGLES:
        return None

    flat = tris.reshape(-1, 3)
    low, high = flat.min(axis=0), flat.max(axis=0)
    rotation = yaw(TURN + turn)
    tris = ((tris - (low + high) / 2) * (PREVIEW_SIZE / max(high - low))) @ rotation.T

    normals = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    lengths = np.linalg.norm(normals, axis=1)
    keep = lengths > 1e-12
    tris, uvs, lights, mats, normals, face_normals = tris[keep], uvs[keep], lights[keep], mats[keep], normals[keep], face_normals[keep]
    normals /= lengths[keep][:, None]
    normals[np.einsum("ij,ij->i", normals, face_normals @ rotation.T) < 0] *= -1

    eye = CAMERA_OFFSET
    forward = -eye / np.linalg.norm(eye)
    right = np.cross([0.0, 1.0, 0.0], forward)
    right /= np.linalg.norm(right)
    up = np.cross(forward, right)
    facing = np.einsum("ij,ij->i", normals, eye - tris.mean(axis=1)) > 0
    tris, uvs, lights, mats, normals = tris[facing], uvs[facing], lights[facing], mats[facing], normals[facing]
    if len(tris) == 0:
        return None

    full = size * SUPERSAMPLE
    focal = full / 2 / math.tan(math.radians(FIELD_OF_VIEW) / 2)
    relative = tris - eye
    depth_of = relative @ forward
    if (depth_of <= NEAR).any():
        return None
    screen_x = full / 2 + (relative @ right) / depth_of * focal
    screen_y = full / 2 - (relative @ up) / depth_of * focal

    diffuse = np.clip(normals @ light_direction(), 0, 1)
    lit = AMBIENT + diffuse[:, None] * LIGHT_COLOR

    color = np.zeros((full, full, 4), dtype=np.float32)
    depth = np.full((full, full), np.inf)
    for t in range(len(tris)):
        (ax, bx, cx), (ay, by, cy), (za, zb, zc) = screen_x[t], screen_y[t], depth_of[t]
        x0, x1 = max(int(math.floor(min(ax, bx, cx))), 0), min(int(math.ceil(max(ax, bx, cx))), full - 1)
        y0, y1 = max(int(math.floor(min(ay, by, cy))), 0), min(int(math.ceil(max(ay, by, cy))), full - 1)
        if x1 < x0 or y1 < y0:
            continue
        area = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
        if abs(area) < 1e-12:
            continue
        xs, ys = np.meshgrid(np.arange(x0, x1 + 1) + 0.5, np.arange(y0, y1 + 1) + 0.5)
        w0 = ((bx - xs) * (cy - ys) - (by - ys) * (cx - xs)) / area
        w1 = ((cx - xs) * (ay - ys) - (cy - ys) * (ax - xs)) / area
        w2 = 1 - w0 - w1
        inside = (w0 >= -1e-6) & (w1 >= -1e-6) & (w2 >= -1e-6)
        if not inside.any():
            continue
        inverse = w0 / za + w1 / zb + w2 / zc
        z = 1 / inverse
        region = depth[y0 : y1 + 1, x0 : x1 + 1]
        closer = inside & (z < region)
        if not closer.any():
            continue
        region[closer] = z[closer]
        p0, p1, p2 = (w0 / za * z)[closer], (w1 / zb * z)[closer], (w2 / zc * z)[closer]
        u = p0 * uvs[t, 0, 0] + p1 * uvs[t, 1, 0] + p2 * uvs[t, 2, 0]
        v = p0 * uvs[t, 0, 1] + p1 * uvs[t, 1, 1] + p2 * uvs[t, 2, 1]
        light = p0 * lights[t, 0] + p1 * lights[t, 1] + p2 * lights[t, 2]
        texels = sample(tiles[mats[t]], u, v)
        shade = lit[t] * (OCCLUSION + (1 - OCCLUSION) * light)[:, None] + emission[mats[t]]
        pixels = color[y0 : y1 + 1, x0 : x1 + 1]
        pixels[closer, :3] = texels[:, :3] * shade
        pixels[closer, 3] = 255

    image = Image.fromarray(np.clip(color, 0, 255).astype(np.uint8), "RGBA")
    return image.convert("RGBa").resize((size, size), Image.LANCZOS).convert("RGBA")


def load_materials(folder):
    info = json.load(open(os.path.join(folder, "materials.json"), encoding="utf-8"))
    tiles = [np.asarray(Image.open(os.path.join(folder, f"{i}.png")).convert("RGBA"), dtype=np.float32) for i in range(info["count"])]
    emission = np.zeros(info["count"])
    for material, strength in info["emission"].items():
        emission[int(material)] = strength
    return tiles, emission, info["fallback"]


_materials = None


def render_file(job):
    global _materials
    source, target, folder, size = job
    if _materials is None:
        _materials = load_materials(folder)
    try:
        with open(source, "rb") as f:
            image = render(f.read(), _materials, size)
        if image is None:
            return "skipped"
        image.save(target, optimize=True)
        return "rendered"
    except Exception as e:
        return f"error {e}"


def packed_model(build, entry, prefix):
    local = f"out/packs/{entry['pack']:03x}.bin"
    if os.path.exists(local):
        with open(local, "rb") as f:
            f.seek(entry["offset"])
            return f.read(entry["length"])
    end = entry["offset"] + entry["length"] - 1
    url = f"{CDN}{build_prefix(build, prefix)}packs/{entry['pack']:03x}.bin"
    request = urllib.request.Request(url, headers={"User-Agent": "games-scan", "Range": f"bytes={entry['offset']}-{end}"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.read()
        except Exception as e:
            error = e
    raise RuntimeError(error)


def fetch(build, entry, folder, prefix):
    path = os.path.join(folder, entry["sha256"] + ".bin")
    if os.path.exists(path):
        return True
    try:
        data = zlib.decompress(packed_model(build, entry, prefix))
    except Exception:
        return False
    with open(path, "wb") as f:
        f.write(data)
    return True


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--models", default="out/models")
    p.add_argument("--materials", default="out/materials")
    p.add_argument("--out", default="out/images")
    p.add_argument("--size", type=int, default=SIZE)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--workers", type=int, default=os.cpu_count() or 4)
    p.add_argument("--catalog", default="", help="top.json from models.py --index, renders its models instead of every file in --models")
    p.add_argument("--shard", type=int, default=0)
    p.add_argument("--shards", type=int, default=1)
    p.add_argument("--upload", action="store_true")
    args = p.parse_args()

    os.makedirs(args.out, exist_ok=True)
    os.makedirs(args.models, exist_ok=True)
    build, prefix = None, PREFIX
    if args.catalog:
        with open(args.catalog, encoding="utf-8") as f:
            catalog = json.load(f)
        build, prefix = catalog["build"], catalog.get("prefix", PREFIX)
        entries = catalog["models"][args.shard :: args.shards]
        if args.limit:
            entries = entries[: args.limit]
        with ThreadPoolExecutor(16) as pool:
            fetched = list(pool.map(lambda entry: fetch(build, entry, args.models, prefix), entries))
        names = [entry["sha256"] + ".bin" for entry, ok in zip(entries, fetched) if ok]
        print(f"shard {args.shard}/{args.shards}: {len(names)} of {len(entries)} models fetched", flush=True)
    else:
        names = sorted(n for n in os.listdir(args.models) if n.endswith(".bin"))
    jobs = [(os.path.join(args.models, n), os.path.join(args.out, n[:-4] + ".png"), args.materials, args.size)
            for n in names if not os.path.exists(os.path.join(args.out, n[:-4] + ".png"))]
    if args.limit:
        jobs = jobs[: args.limit]
    results = {}
    with ProcessPoolExecutor(args.workers) as pool:
        for result in pool.map(render_file, jobs, chunksize=4):
            key = result.split(" ")[0]
            results[key] = results.get(key, 0) + 1
    print(f"{len(jobs)} models: {results}", flush=True)

    if args.upload and build:
        s3 = client()
        images = [n[:-4] + ".png" for n in names if os.path.exists(os.path.join(args.out, n[:-4] + ".png"))]

        def send(name):
            with open(os.path.join(args.out, name), "rb") as f:
                upload(s3, build_prefix(build, prefix) + "images/" + name, f.read(), "image/png", IMMUTABLE)

        with ThreadPoolExecutor(16) as pool:
            list(pool.map(send, images))
        print(f"{len(images)} images uploaded" if s3 else "no R2 credentials, nothing uploaded")


if __name__ == "__main__":
    main()
