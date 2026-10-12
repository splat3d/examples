#!/usr/bin/env python3
"""Mip-NeRF 360 on the Splat3D API: a scene folder in, a 3D scene and its metrics out.

    zip images_4/ (+ sparse/0/) -> create -> upload -> process -> poll -> download -> metrics

The steps after the zip are the Python quickstart's (../../python/quickstart.py),
imported, not copied. Standard library only, Python 3.10+. The README next to
this file says where to get the dataset; this repository holds none of it.

    export SPLAT3D_API_KEY="s3d_..."
    python3 run.py ~/data/360_v2/garden
    python3 run.py ~/data/360_v2/garden --with-colmap
"""

from __future__ import annotations

import argparse
import struct
import sys
import tempfile
import zipfile
from pathlib import Path

# The quickstart lives two folders up, in python/.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))
import quickstart  # noqa: E402

RUNNER = "python3 run.py"
IMAGE_FOLDERS = ("images", "images_2", "images_4", "images_8")
DEFAULT_IMAGES = "images_4"
DEFAULT_PRESET = "standard"  # the cheapest preset that holds photos out for psnr_holdout
PHOTO_SUFFIXES = (".jpg", ".jpeg", ".png")
EXIT_USAGE = 2

# The API's limits. The largest single upload storage accepts is
# 5 GiB - 5 MiB; a COLMAP model may register at most 800 photos, and a
# photo set past 800 is thinned to 800.
UPLOAD_LIMIT_BYTES = 5 * 1024**3 - 5 * 1024**2
MAX_PHOTOS = 800

# COLMAP's sparse model, in the layout the API reads as "skip SfM":
# images/ beside sparse/0/ holding these three files.
SPARSE_DIR = Path("sparse") / "0"
SPARSE_FILES = ("cameras.bin", "images.bin", "points3D.bin")
ZIP_PHOTO_DIR = "images"

# Parameters per COLMAP camera model id, for the models the API trains.
CAMERA_PARAMS = {0: 3, 1: 4, 2: 4, 3: 5, 4: 8}  # SIMPLE_PINHOLE, PINHOLE, SIMPLE_RADIAL, RADIAL, OPENCV
IMAGE_POINT_BYTES = 24  # one 2D point in images.bin: x, y (double) and a point3D id (int64)

# A photo must be its camera's size or a uniform resize of it, within a
# pixel of rounding, or the API refuses the model.
MAX_RESIZE_ERROR_PX = 1.0
MISMATCH_EXAMPLES = 3

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_START = b"\xff\xd8"
JPEG_MARKER = 0xFF
# Start-of-frame markers, which carry the size: C0-CF but C4 (DHT), C8 (JPG) and CC (DAC).
JPEG_FRAME_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}

METRICS_NOTE = (
    "note: Splat3D computes these on its own split: about 10% of the photos, evenly spaced,\n"
    "are held out of training (none at the fast preset), at the preset's resolution and\n"
    "training length. Published Mip-NeRF 360 results use their own test split and settings,\n"
    "so these numbers are not directly comparable with them."
)


class Unusable(Exception):
    """The scene folder can't make an upload the API accepts. A usage error: exit 2."""


# -- reading the scene folder ---------------------------------------------------


def photos_in(folder: Path) -> list[Path]:
    """The JPEG and PNG files in a folder, by name. Hidden files are skipped."""
    if not folder.is_dir():
        raise Unusable(f"no {folder.name}/ folder in {folder.parent}")

    photos = sorted(
        path for path in folder.iterdir()
        if path.is_file() and path.suffix.lower() in PHOTO_SUFFIXES and not path.name.startswith(".")
    )
    if len(photos) > MAX_PHOTOS:
        raise Unusable(f"{folder} has {len(photos)} photos; the API takes at most {MAX_PHOTOS}")
    return photos


def photo_size(path: Path) -> tuple[int, int]:
    """A photo's stored width and height, read from its header (PNG IHDR or JPEG start-of-frame)."""
    with path.open("rb") as file:
        head = file.read(len(PNG_SIGNATURE) + 16)
        if head.startswith(PNG_SIGNATURE):
            return struct.unpack(">II", head[16:24])
        if not head.startswith(JPEG_START):
            raise Unusable(f"{path.name} is not a JPEG or PNG")

        # Walk the segments: FF <marker> <length> <payload>, until a frame header.
        file.seek(len(JPEG_START))
        while True:
            byte = file.read(1)
            if not byte:
                break
            if byte[0] != JPEG_MARKER:
                continue

            marker = file.read(1)
            while marker and marker[0] == JPEG_MARKER:  # fill bytes
                marker = file.read(1)
            if not marker:
                break
            if marker[0] in JPEG_FRAME_MARKERS:
                file.read(3)  # segment length, sample precision
                height, width = struct.unpack(">HH", file.read(4))
                return width, height

            (length,) = struct.unpack(">H", file.read(2))
            file.seek(length - 2, 1)

    raise Unusable(f"can't read the size of {path.name}")


def read_cameras(path: Path) -> dict[int, tuple[int, int]]:
    """{camera id: (width, height)} from a COLMAP cameras.bin."""
    data = path.read_bytes()
    (count,) = struct.unpack_from("<Q", data, 0)
    offset = 8
    cameras = {}
    for _ in range(count):
        camera_id, model, width, height = struct.unpack_from("<iiQQ", data, offset)
        if model not in CAMERA_PARAMS:
            raise Unusable(f"cameras.bin uses camera model id {model}; the API trains PINHOLE, SIMPLE_PINHOLE, "
                           "SIMPLE_RADIAL, RADIAL and OPENCV")
        offset += 24 + 8 * CAMERA_PARAMS[model]
        cameras[camera_id] = (width, height)
    return cameras


def read_registered(path: Path) -> dict[str, int]:
    """{photo name: camera id} for every image a COLMAP images.bin registers."""
    data = path.read_bytes()
    (count,) = struct.unpack_from("<Q", data, 0)
    offset = 8
    registered = {}
    for _ in range(count):
        # image id, rotation (4 doubles), translation (3 doubles), camera id
        (camera_id,) = struct.unpack_from("<i", data, offset + 4 + 7 * 8)
        offset += 4 + 7 * 8 + 4
        end = data.index(b"\x00", offset)
        name = data[offset:end].decode()
        offset = end + 1
        (points,) = struct.unpack_from("<Q", data, offset)
        offset += 8 + points * IMAGE_POINT_BYTES
        registered[name] = camera_id
    return registered


def resize_error(camera: tuple[int, int], photo: tuple[int, int]) -> float:
    """Pixels by which a photo misses being a uniform resize of its camera, as the API measures it.

    Garden's 5187x3361 camera with its 1297x840 images_4 photos misses by 0.41 px: accepted.
    """
    width, height = camera
    photo_w, photo_h = photo
    cross = abs(photo_w * height - photo_h * width)
    if width >= height:
        return cross / max(width, photo_w)
    return cross / max(height, photo_h)


def check_colmap(scene: Path, folder: Path) -> list[Path]:
    """Check sparse/0/ against the photos the way the API will. Returns the three model files."""
    sparse = scene / SPARSE_DIR
    model = [sparse / name for name in SPARSE_FILES]
    missing = [path.name for path in model if not path.is_file()]
    if missing:
        raise Unusable(f"--with-colmap needs {', '.join(missing)} in {sparse}")

    cameras = read_cameras(sparse / "cameras.bin")
    registered = read_registered(sparse / "images.bin")
    if len(registered) > MAX_PHOTOS:
        raise Unusable(f"the model registers {len(registered)} photos; the API takes at most {MAX_PHOTOS}")

    absent = [name for name in registered if not (folder / name).is_file()]
    if absent:
        raise Unusable(f"images.bin names {len(absent)} photo(s) not in {folder.name}/, e.g. {absent[0]}")

    unknown = sorted({camera_id for camera_id in registered.values()} - set(cameras))
    if unknown:
        raise Unusable(f"images.bin names camera {unknown[0]}, which cameras.bin doesn't have")

    mismatches = []
    for name, camera_id in registered.items():
        camera = cameras[camera_id]
        photo = photo_size(folder / name)
        if resize_error(camera, photo) > MAX_RESIZE_ERROR_PX:
            mismatches.append(f"{name} is {photo[0]}x{photo[1]}, camera {camera_id} is {camera[0]}x{camera[1]}")
    if mismatches:
        raise Unusable(
            f"{len(mismatches)} photo(s) in {folder.name}/ are not a uniform resize of their camera, "
            f"so the API would refuse the model: {'; '.join(mismatches[:MISMATCH_EXAMPLES])}"
        )
    return model


# -- the zip --------------------------------------------------------------------


def zip_entries(scene: Path, args: argparse.Namespace) -> list[tuple[str, Path]]:
    """(name in the zip, file) for every file to upload: images/<photo>, then sparse/0/<model file>."""
    folder = scene / args.images
    entries = [(f"{ZIP_PHOTO_DIR}/{photo.name}", photo) for photo in photos_in(folder)]
    if args.with_colmap:
        entries += [(f"{SPARSE_DIR.as_posix()}/{path.name}", path) for path in check_colmap(scene, folder)]

    total = sum(path.stat().st_size for _, path in entries)
    if total > UPLOAD_LIMIT_BYTES:
        raise Unusable(f"{total} bytes is over the API's 5 GB upload limit; try a smaller --images folder")
    return entries


def write_zip(entries: list[tuple[str, Path]], target: Path) -> Path:
    """Store the files uncompressed: JPEGs don't shrink, and storing is fast."""
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, path in entries:
            archive.write(path, name)

    if target.stat().st_size > UPLOAD_LIMIT_BYTES:
        raise Unusable(f"{target.name} is over the API's 5 GB upload limit; try a smaller --images folder")
    quickstart.log(f"zipped {len(entries)} files into {target.name} ({target.stat().st_size} bytes)")
    return target


# -- the run --------------------------------------------------------------------


def report(scene: dict) -> None:
    """Print the scene's metrics and what they can be compared with."""
    print("metrics:")
    if scene.get("psnr_holdout") is not None:
        print(f"  psnr_holdout  {scene['psnr_holdout']} dB, on held-out photos")
    if scene.get("ssim_holdout") is not None:
        print(f"  ssim_holdout  {scene['ssim_holdout']}, on held-out photos")
    if scene.get("ssim") is not None:
        print(f"  ssim          {scene['ssim']}, on the training photos")
    if scene.get("psnr_holdout") is None:
        print("  no photos were held out (the fast preset holds none out): use --preset standard or higher")
    print(METRICS_NOTE, flush=True)


def run(api: quickstart.Api, args: argparse.Namespace) -> int:
    scene = quickstart.finish(api, quickstart.launch(api, args), args)
    if scene is None:
        return quickstart.EXIT_FAILED
    report(scene)
    return quickstart.EXIT_OK


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reconstruct a Mip-NeRF 360 scene with the Splat3D API and print its metrics.",
        epilog="Environment: SPLAT3D_API_KEY (required), SPLAT3D_API_BASE, SPLAT3D_POLL_SECONDS.",
    )
    parser.add_argument("scene", nargs="?", help="a scene folder you downloaded, e.g. ~/data/360_v2/garden")
    parser.add_argument("--images", choices=IMAGE_FOLDERS, default=DEFAULT_IMAGES,
                        help=f"which photo folder to upload (default: {DEFAULT_IMAGES})")
    parser.add_argument("--with-colmap", action="store_true",
                        help="also upload sparse/0/, so the API uses the dataset's poses and skips SfM")
    parser.add_argument("--preset", choices=quickstart.PRESETS, default=DEFAULT_PRESET)
    parser.add_argument("--format", choices=quickstart.FORMATS, default="spz")
    parser.add_argument("--out", default=quickstart.DEFAULT_OUT, help="directory for the model (default: .)")
    parser.add_argument("--scene-id", help="resume a scene instead of creating one")
    args = parser.parse_args()

    # Usage errors exit 2 before any request.
    if bool(args.scene) == bool(args.scene_id):
        parser.error("give a scene folder or --scene-id, not both")
    quickstart.read_env(parser, args)
    args.runner = RUNNER

    args.entries = []
    if args.scene:
        scene = Path(args.scene).expanduser()
        try:
            args.entries = zip_entries(scene, args)
        except Unusable as err:
            parser.error(str(err))
        except (struct.error, ValueError):
            # A file cut short, or not a binary COLMAP model (a text export, say).
            parser.error(f"can't read the COLMAP model in {scene / SPARSE_DIR}; it must be COLMAP's binary format")

        suffix = "-colmap" if args.with_colmap else ""
        args.zip_name = f"{scene.resolve().name}-{args.images}{suffix}.zip"
    return args


def main() -> int:
    args = parse_args()
    api = quickstart.Api(args.api_base, args.key)

    # The zip lives only until the run ends: once uploaded, the scene no longer needs it.
    with tempfile.TemporaryDirectory(prefix="splat3d-mipnerf360-") as tmp:
        if args.entries:
            try:
                args.file = str(write_zip(args.entries, Path(tmp) / args.zip_name))
            except Unusable as err:
                quickstart.log(f"error: {err}")
                return EXIT_USAGE
        return quickstart.guard(run, api, args)


if __name__ == "__main__":
    sys.exit(main())
