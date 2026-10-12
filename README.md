# Splat3D API examples

Turn a phone video or a set of photos into a 3D Gaussian splat with the
[Splat3D API](https://docs.splat-3d.com). One file each in curl, Python and JavaScript, plus a
benchmark run on the Mip-NeRF 360 dataset. No dependencies: Python's standard library, Node's
built-ins, or bash with curl and jq.

| Example | Needs | |
|---|---|---|
| [`curl/quickstart.sh`](curl/) | bash 3.2+, curl, jq | your video or zip of photos in, a 3D model out |
| [`python/quickstart.py`](python/) | Python 3.10+ | the same |
| [`javascript/quickstart.mjs`](javascript/) | Node 22+ | the same |
| [`benchmarks/mipnerf360/run.py`](benchmarks/mipnerf360/) | Python 3.10+ | a Mip-NeRF 360 scene in, a 3D model and its metrics out |

The API reference is at [docs.splat-3d.com](https://docs.splat-3d.com).

Building with a coding agent (Claude Code, Cursor, Codex)? Load [`skills/splat3d-api/SKILL.md`](skills/splat3d-api/SKILL.md): the API loop and the mistakes agents make, in one file.

## Run a quickstart

1. Create an API key at [splat-3d.com/dashboard/developer](https://splat-3d.com/dashboard/developer).
2. Record a 10 to 30 second video orbiting an object, or zip 5 or more JPEG or PNG photos of it.
   Up to 5 GB. For photos to try, see the [Mip-NeRF 360 benchmark](benchmarks/mipnerf360/).
3. Run:

```bash
export SPLAT3D_API_KEY="s3d_..."
python3 python/quickstart.py capture.mp4
# or: node javascript/quickstart.mjs capture.mp4
# or: ./curl/quickstart.sh capture.mp4
```

A scene at the default `fast` preset takes several minutes. You get `scene-<id>.spz` in the
current directory and a link to view it on splat-3d.com.

**Cost:** `fast` costs 10 credits ($1). `standard` costs 20, `quality` 40, `ultra` 70. Signup grants
100 credits once. A scene that fails is refunded automatically.

## How it works

```
  create ──> upload ──> process ──> poll ──> download
  POST       PUT        POST        GET      GET
  /v1/scenes uploadUrl  /{id}/      /{id}    /{id}/download?format=spz
             (storage,  process     every    ──> scene-<id>.spz
             no key)                10 s
```

1. **Create** a scene: `POST /v1/scenes` with the file's `content_type`, `file_size` and a `preset`.
   The answer holds `sceneId` and `uploadUrl`, a presigned URL that lasts 24 hours.
2. **Upload** the file with `PUT uploadUrl`. Send the same `Content-Type` you gave at create (the URL
   signs it) and a `Content-Length`. Send no API key: the URL is the credential.
3. **Process**: `POST /v1/scenes/{id}/process` with the body `{}`. This charges the credits. Sending
   the same request again replays the first answer and charges nothing. Its `status` is always
   `processing`: it means the launch was accepted.
4. **Poll** `GET /v1/scenes/{id}` until `status` is `complete`, `failed` or `cancelled`. While the
   scene processes, `status` is the pipeline stage it is in, e.g. `extracting_frames` or
   `training`, and `processing_stage` and `processing_pct` show progress. Both are `null` once it
   stops.
5. **Download** `GET /v1/scenes/{id}/download?format=spz`. The `X-Splat-Format` header names the
   file sent: `sog` and `ply` fall back to each other.

## Options

All three quickstarts take the same arguments and exit codes.

```
quickstart FILE [--preset fast] [--format spz] [--out DIR]
quickstart --scene-id ID [--format spz] [--out DIR]
```

| Option | Default | |
|---|---|---|
| `FILE` | | `.mp4`, `.mov` or `.zip` (photos), up to 5 GB |
| `--preset` | `fast` | `fast`, `standard`, `quality` or `ultra` |
| `--format` | `spz` | `spz` for native viewers, `sog` for web viewers, `ply` for full precision |
| `--out` | `.` | where the model is saved |
| `--scene-id` | | resume a scene: skip create and upload, then poll and download. A scene that is still `uploading` is started first |

| Environment | Default | |
|---|---|---|
| `SPLAT3D_API_KEY` | | required |
| `SPLAT3D_API_BASE` | `https://api.splat-3d.com` | |
| `SPLAT3D_POLL_SECONDS` | `10` | seconds between polls |

| Exit | Meaning |
|---|---|
| `0` | the model is saved |
| `1` | the API refused, the scene failed or was cancelled, or the wait ran out |
| `2` | usage error: missing key, unknown option, unsupported file |

## Errors

Every error answer has the same shape. The examples print `code`, `message`, `request_id` and
each `details` entry; quote the `request_id` when you ask for help.

```json
{ "error": { "code": "source_unusable", "message": "...",
             "details": [ { "field": "photo_count", "value": 3, "allowed": { "min": 5 } } ] },
  "meta": { "request_id": "550e8400-e29b-41d4-a716-446655440000" } }
```

A request that fails validation lists one `{ "field", "message" }` per problem in `details`
instead.

| Answer | When | What the examples do |
|---|---|---|
| 429 `queue_full` | as many of your scenes are processing as your plan allows | nothing was charged: wait `Retry-After` seconds and send `/process` again, for up to 30 minutes |
| 402 `insufficient_credits` | not enough credits for the preset | nothing was charged: point to the billing page and print the `--scene-id` command to run after adding credits |
| 422 `source_unusable` | the file can't be reconstructed, e.g. a zip of 3 photos | print `details`; nothing was charged |
| 413 `payload_too_large` | the upload is over 5 GB | stop; a file declared over 5 GB at create is refused there, with 400 `invalid_input` |
| 429 `quota_exceeded` | the monthly quota is used up | stop |
| 503 with `Retry-After` | a passing outage on poll, `/process` or download | wait and retry, up to 5 times |
| 503 on create | the same, but create is not safe to repeat | stop; check `GET /v1/scenes` before creating again |
| 404 on `format=spz` | the scene has no SPZ file | stop and suggest `--format sog` |
| 400, 401, 404, 409 | bad input, bad key, no scene, already processing | stop |

A scene that ends `failed` prints its `processing_error`. A wait that runs out (30 minutes in the
queue, or 150 minutes processing) prints the `--scene-id` command that picks it up again.

## Tests

The tests run each example against a local mock of the API, no key needed:

```bash
python3 -m unittest discover -s test -v
```

Never put an API key in a browser or a mobile app bundle. The JavaScript example is for Node.

MIT licensed.
