"""Run the Mip-NeRF 360 benchmark against the mock API on a tiny synthetic scene.

    python3 -m unittest discover -s test -v

The scene folder is made up here: a few header-only JPEGs and a small COLMAP
model, laid out like a Mip-NeRF 360 scene. No real dataset images.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from mock_api import API_KEY, METRICS, MODEL_BYTES, SCENE_ID, MockApi

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "benchmarks" / "mipnerf360" / "run.py"
TIMEOUT_SECONDS = 60
POLL_SECONDS = "0.05"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

CREATE = "/v1/scenes"
UPLOAD = f"/upload/{SCENE_ID}"

PHOTOS = ("DSC0001.JPG", "DSC0002.JPG", "DSC0003.JPG", "DSC0004.JPG", "DSC0005.JPG", "DSC0006.JPG")
CAMERA_SIZE = (400, 300)
# Each folder's photo size. A Mip-NeRF 360 downscale rounds each axis, so
# 400x300 / 8 is 50x38 (37.5 rounded), a uniform resize within a pixel.
FOLDER_SIZES = {"images": (400, 300), "images_2": (200, 150), "images_4": (100, 75), "images_8": (50, 38)}
SPARSE_FILES = ("cameras.bin", "images.bin", "points3D.bin")
COLMAP_PINHOLE = 1
MAX_PHOTOS = 800


def jpeg(size: tuple[int, int]) -> bytes:
    """A header-only JPEG: SOI, an APP0 segment, a baseline SOF0 frame of `size`, EOI."""
    width, height = size
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def cameras_bin(size: tuple[int, int]) -> bytes:
    """One PINHOLE camera (id 1) of `size`."""
    width, height = size
    return struct.pack("<Q", 1) + struct.pack("<iiQQ", 1, COLMAP_PINHOLE, width, height) + struct.pack("<4d", 300.0, 300.0, 200.0, 150.0)


def images_bin(names: tuple[str, ...]) -> bytes:
    """One registered image per name, all on camera 1, with no 2D points."""
    records = b"".join(
        struct.pack("<i4d3di", index + 1, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1) + name.encode() + b"\x00" + struct.pack("<Q", 0)
        for index, name in enumerate(names)
    )
    return struct.pack("<Q", len(names)) + records


class MipNerf360(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="splat3d-mipnerf360-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.scene = self.tmp / "garden"
        self.out = self.tmp / "out"

        for folder, size in FOLDER_SIZES.items():
            (self.scene / folder).mkdir(parents=True)
            for name in PHOTOS:
                (self.scene / folder / name).write_bytes(jpeg(size))

        sparse = self.scene / "sparse" / "0"
        sparse.mkdir(parents=True)
        (sparse / "cameras.bin").write_bytes(cameras_bin(CAMERA_SIZE))
        (sparse / "images.bin").write_bytes(images_bin(PHOTOS))
        (sparse / "points3D.bin").write_bytes(b"points3D stand-in")
        (sparse / "project.ini").write_text("not part of the model\n")
        (self.scene / "poses_bounds.npy").write_bytes(b"not uploaded")

    def run_benchmark(self, scenario: str, *args: str) -> subprocess.CompletedProcess:
        """Run run.py against a fresh mock; keep the mock as self.mock."""
        with MockApi(scenario) as mock:
            env = {
                **os.environ,
                "SPLAT3D_API_KEY": API_KEY,
                "SPLAT3D_API_BASE": mock.base_url,
                "SPLAT3D_POLL_SECONDS": POLL_SECONDS,
                "NO_PROXY": "127.0.0.1",
                "no_proxy": "127.0.0.1",
            }
            result = subprocess.run(
                [sys.executable, str(RUN), *args, "--out", str(self.out)], env=env, cwd=self.tmp,
                capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
            )
        self.mock = mock
        return result

    def assertExit(self, result: subprocess.CompletedProcess, code: int) -> None:
        self.assertEqual(result.returncode, code, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")

    def uploaded_zip(self) -> zipfile.ZipFile:
        puts = [c for c in self.mock.calls if c.method == "PUT" and c.path == UPLOAD]
        self.assertEqual(len(puts), 1)
        return zipfile.ZipFile(io.BytesIO(puts[0].body))

    def create_body(self) -> dict:
        creates = [c for c in self.mock.calls if c.method == "POST" and c.path == CREATE]
        self.assertEqual(len(creates), 1)
        return json.loads(creates[0].body)

    def photo_names(self) -> list[str]:
        return [f"images/{name}" for name in PHOTOS]

    # -- the zip --------------------------------------------------------------

    def test_photos_only(self) -> None:
        result = self.run_benchmark("happy", str(self.scene))

        self.assertExit(result, EXIT_OK)
        archive = self.uploaded_zip()
        self.assertEqual(sorted(archive.namelist()), self.photo_names())
        for name in PHOTOS:
            self.assertEqual(archive.read(f"images/{name}"), (self.scene / "images_4" / name).read_bytes())

        body = self.create_body()
        self.assertEqual(body["content_type"], "application/zip")
        self.assertEqual(body["title"], "garden-images_4.zip")
        self.assertEqual(body["preset"], "standard")
        self.assertEqual(self.mock.upload_problems, [])
        self.assertEqual((self.out / f"scene-{SCENE_ID}.spz").read_bytes(), MODEL_BYTES["spz"])

    def test_with_colmap(self) -> None:
        result = self.run_benchmark("happy", str(self.scene), "--with-colmap")

        self.assertExit(result, EXIT_OK)
        archive = self.uploaded_zip()
        sparse = [f"sparse/0/{name}" for name in SPARSE_FILES]
        self.assertEqual(sorted(archive.namelist()), sorted(self.photo_names() + sparse))
        for name in SPARSE_FILES:
            self.assertEqual(archive.read(f"sparse/0/{name}"), (self.scene / "sparse" / "0" / name).read_bytes())
        self.assertEqual(self.create_body()["title"], "garden-images_4-colmap.zip")

    def test_images_choice(self) -> None:
        for folder in FOLDER_SIZES:
            with self.subTest(folder=folder):
                result = self.run_benchmark("happy", str(self.scene), "--with-colmap", "--images", folder)

                self.assertExit(result, EXIT_OK)
                archive = self.uploaded_zip()
                self.assertEqual(archive.read(f"images/{PHOTOS[0]}"), (self.scene / folder / PHOTOS[0]).read_bytes())

    # -- refused before any request ---------------------------------------------

    def test_colmap_size_mismatch(self) -> None:
        # Square photos can't be a uniform resize of a 4:3 camera.
        (self.scene / "images_4" / PHOTOS[1]).write_bytes(jpeg((100, 100)))

        result = self.run_benchmark("happy", str(self.scene), "--with-colmap")

        self.assertExit(result, EXIT_USAGE)
        self.assertIn(f"{PHOTOS[1]} is 100x100", result.stderr)
        self.assertIn("400x300", result.stderr)
        self.assertEqual(self.mock.calls, [])

    def test_photos_only_skips_the_size_check(self) -> None:
        # Without a COLMAP model the API runs its own SfM: any size goes.
        (self.scene / "images_4" / PHOTOS[1]).write_bytes(jpeg((100, 100)))

        result = self.run_benchmark("happy", str(self.scene))

        self.assertExit(result, EXIT_OK)

    def test_colmap_photo_missing(self) -> None:
        (self.scene / "images_4" / PHOTOS[2]).unlink()

        result = self.run_benchmark("happy", str(self.scene), "--with-colmap")

        self.assertExit(result, EXIT_USAGE)
        self.assertIn(PHOTOS[2], result.stderr)
        self.assertEqual(self.mock.calls, [])

    def test_colmap_model_missing(self) -> None:
        (self.scene / "sparse" / "0" / "points3D.bin").unlink()

        result = self.run_benchmark("happy", str(self.scene), "--with-colmap")

        self.assertExit(result, EXIT_USAGE)
        self.assertIn("points3D.bin", result.stderr)
        self.assertEqual(self.mock.calls, [])

    def test_folder_missing(self) -> None:
        shutil.rmtree(self.scene / "images_8")

        result = self.run_benchmark("happy", str(self.scene), "--images", "images_8")

        self.assertExit(result, EXIT_USAGE)
        self.assertIn("images_8", result.stderr)
        self.assertEqual(self.mock.calls, [])

    def test_too_many_photos(self) -> None:
        folder = self.scene / "images_4"
        for index in range(MAX_PHOTOS + 1 - len(PHOTOS)):
            (folder / f"extra{index:04d}.JPG").write_bytes(jpeg(FOLDER_SIZES["images_4"]))

        result = self.run_benchmark("happy", str(self.scene))

        self.assertExit(result, EXIT_USAGE)
        self.assertIn(str(MAX_PHOTOS), result.stderr)
        self.assertEqual(self.mock.calls, [])

    # -- metrics ----------------------------------------------------------------

    def test_metrics(self) -> None:
        result = self.run_benchmark("happy", str(self.scene))

        self.assertExit(result, EXIT_OK)
        self.assertIn(f"psnr_holdout  {METRICS['psnr_holdout']}", result.stdout)
        self.assertIn(f"ssim_holdout  {METRICS['ssim_holdout']}", result.stdout)
        self.assertIn(f"ssim          {METRICS['ssim']}", result.stdout)
        self.assertIn("not directly comparable", result.stdout)

    def test_no_holdout(self) -> None:
        result = self.run_benchmark("no_holdout", str(self.scene), "--preset", "fast")

        self.assertExit(result, EXIT_OK)
        self.assertNotIn("psnr_holdout  ", result.stdout)
        self.assertIn("no photos were held out", result.stdout)

    def test_resume(self) -> None:
        result = self.run_benchmark("resume", "--scene-id", SCENE_ID)

        self.assertExit(result, EXIT_OK)
        self.assertEqual([c for c in self.mock.calls if c.method in ("POST", "PUT")], [])
        self.assertIn(f"psnr_holdout  {METRICS['psnr_holdout']}", result.stdout)

    def test_resume_command_names_run_py(self) -> None:
        result = self.run_benchmark("queue_full_long", str(self.scene))

        self.assertExit(result, EXIT_FAILED)
        self.assertIn(f"resume: python3 run.py --scene-id {SCENE_ID}", result.stderr)


if __name__ == "__main__":
    unittest.main()
