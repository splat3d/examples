# Mip-NeRF 360 on the Splat3D API

Reconstruct a scene from the Mip-NeRF 360 dataset and print its metrics. Needs Python 3.10+;
standard library only. It reuses the Python quickstart's client (`../../python/quickstart.py`),
so keep the two folders together.

## Get the dataset

Download it from the [Mip-NeRF 360 project page](https://jonbarron.info/mipnerf360/).
"Dataset Pt. 1" (`360_v2.zip`, 12.5 GB) holds seven scenes: bicycle, bonsai, counter, garden,
kitchen, room and stump. "Dataset Pt. 2" (`360_extra_scenes.zip`, 4.5 GB) holds flowers and
treehill. Each scene folder holds `images/` (full resolution), `images_2/`, `images_4/` and
`images_8/` (downscaled 2, 4 and 8 times), and a COLMAP model in `sparse/0/`.

The dataset page states no license, so this repository includes none of its images. The tests
use a tiny made-up scene.

```bash
unzip 360_v2.zip -d ~/data/360_v2
```

## Run

```bash
export SPLAT3D_API_KEY="s3d_..."
python3 run.py ~/data/360_v2/garden                    # photos only: the API runs its own SfM
python3 run.py ~/data/360_v2/garden --with-colmap      # the dataset's poses: SfM is skipped
python3 run.py --scene-id a1b2c3d4e5f6                 # resume a scene
```

It zips the photos, then creates, uploads, processes, polls and downloads the same way the
quickstart does, and prints the scene's metrics.

| Option | Default | |
|---|---|---|
| `SCENE` | | a scene folder, e.g. `~/data/360_v2/garden` |
| `--images` | `images_4` | `images`, `images_2`, `images_4` or `images_8` |
| `--with-colmap` | off | also upload `sparse/0/`, so the API uses the dataset's camera poses |
| `--preset` | `standard` | `fast` holds no photos out, so it reports no held-out metrics |
| `--format`, `--out`, `--scene-id` | | as in the quickstart |

Environment variables and exit codes are the quickstart's (see the top-level README).

## What it uploads

```
garden-images_4.zip                 garden-images_4-colmap.zip (--with-colmap)
└── images/DSC07956.JPG ...         ├── images/DSC07956.JPG ...
                                    └── sparse/0/cameras.bin
                                                 images.bin
                                                 points3D.bin
```

A zip with `images/` beside `sparse/0/` tells the API to use that COLMAP model as it is and skip
structure from motion. The API refuses the model when a photo is not its camera's size or a
uniform resize of it, within a pixel. The dataset's `sparse/0/` describes the full-resolution
photos (garden: 5187x3361), and every downscaled folder passes: garden's `images_4` photos are
1297x840, 0.41 px off. `run.py` makes the same check before it uploads, and stops if a photo
fails it.

Limits: at most 800 photos, and 5 GB per upload. Every scene in both archives fits, even at full
resolution: the largest `images/` folder is garden's, 2.0 GB, and room has the most photos, 311.

## Metrics

```
metrics:
  psnr_holdout  ... dB, on held-out photos
  ssim_holdout  ..., on held-out photos
```

The API holds out about 10% of the photos, evenly spaced, from training at the `standard`,
`quality` and `ultra` presets, and measures PSNR and SSIM on them at the preset's resolution and
training length. Published Mip-NeRF 360 results use their own test split and settings, so these
numbers are **not directly comparable** with them. A scene also has an `ssim` field for the
training photos; `run.py` prints it when the API sets it.

**Cost:** `standard` costs 20 credits ($2). The other presets cost as in the quickstart.
