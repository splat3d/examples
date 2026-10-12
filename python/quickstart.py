#!/usr/bin/env python3
"""Splat3D quickstart: a video or a zip of photos in, a 3D scene file out.

    create -> upload -> process -> poll -> download

Standard library only, Python 3.10+. Options, exit codes and errors are in
../README.md.

    export SPLAT3D_API_KEY="s3d_..."
    python3 quickstart.py capture.mp4
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shlex
import shutil
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn
from urllib.parse import quote

DEFAULT_API_BASE = "https://api.splat-3d.com"
KEYS_PAGE = "https://splat-3d.com/dashboard/developer"
BILLING_PAGE = "https://splat-3d.com/dashboard/billing"
USER_AGENT = "splat3d-examples/0.1 (python)"
RUNNER = "python3 quickstart.py"

DEFAULT_POLL_SECONDS = 10
MAX_WAIT_MINUTES = 150  # the API fails a scene still processing after this
MAX_QUEUE_WAIT_MINUTES = 30
MAX_UNAVAILABLE_RETRIES = 5
HTTP_TIMEOUT_SECONDS = 60
ERROR_PREVIEW_BYTES = 500
TITLE_MAX_CHARS = 200
SECONDS_PER_MINUTE = 60

EXIT_OK = 0
EXIT_FAILED = 1  # usage errors exit 2, from argparse

HTTP_NOT_FOUND = 404
HTTP_UNAVAILABLE = 503

CONTENT_TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime", ".zip": "application/zip"}
PRESETS = ("fast", "standard", "quality", "ultra")
FORMATS = ("spz", "sog", "ply")
DEFAULT_OUT = "."

UPLOADING = "uploading"
NO_REASON = "no reason given"
TERMINAL_STATUSES = ("complete", "failed", "cancelled")
# Every other status is a pipeline stage, e.g. extracting_frames or training.
IDLE_STATUSES = (UPLOADING, *TERMINAL_STATUSES)

# Extra advice for an error code, printed under the error.
HINTS = {
    "source_unusable": "run again with a usable file",
    "payload_too_large": "the upload limit is 5 GB; send a shorter video or fewer photos",
    "unauthorized": f"check SPLAT3D_API_KEY; keys are at {KEYS_PAGE}",
}


def log(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


class ApiError(Exception):
    """A non-2xx answer: the error envelope's fields, or the start of the raw body."""

    def __init__(self, status: int, headers, body: bytes) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.retry_after = retry_after(headers.get("Retry-After"))
        self.raw = body[:ERROR_PREVIEW_BYTES].decode("utf-8", "replace")
        self.code: str | None = None
        self.message = ""
        self.details: list = []
        self.request_id = None

        # { error: { code, message, details? }, meta: { request_id } }
        try:
            envelope = json.loads(body)
            self.code = envelope["error"]["code"]
            self.message = envelope["error"]["message"]
            self.details = envelope["error"].get("details") or []
            self.request_id = envelope.get("meta", {}).get("request_id") or "unknown"
        except (ValueError, KeyError, TypeError, AttributeError):
            self.code = None  # not the envelope, e.g. a proxy's page

    def report(self) -> None:
        if self.code is None:
            log(f"error: HTTP {self.status}: {self.raw}")
            return

        log(f"error: HTTP {self.status} {self.code}: {self.message} (request {self.request_id})")
        for detail in self.details:
            log(f"  {describe(detail)}")


def retry_after(value: str | None) -> int | None:
    """Retry-After in seconds. The API sends whole seconds, never a date."""
    if value is None or not value.strip().isdigit():
        return None
    return int(value)


def describe(detail: dict) -> str:
    """One `details` entry, in either of its two shapes.

    A request that fails validation: `preset: Invalid enum value. ...`
    A file that can't be used: `photo_count: 3 (allowed {"min":5}) Add JPEG or PNG photos.`
    """
    if "message" in detail:
        return f"{detail.get('field')}: {detail['message']}"

    allowed = json.dumps(detail.get("allowed"), separators=(",", ":"))
    text = f"{detail.get('field')}: {detail.get('value')} (allowed {allowed})"
    hint = detail.get("hint")
    return f"{text} {hint}" if hint else text


def fail(err: ApiError, *hints: str) -> NoReturn:
    """Report the error, print each hint, and exit 1."""
    err.report()
    for hint in hints:
        log(hint)
    sys.exit(EXIT_FAILED)


def send(request: urllib.request.Request):
    """Open a request; a non-2xx answer raises ApiError."""
    try:
        return urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS)
    except urllib.error.HTTPError as err:
        with err:
            raise ApiError(err.code, err.headers, err.read()) from None


def read_data(response) -> dict:
    with response:
        return json.load(response)["data"]


class Api:
    """The Splat3D API: bearer key, a named User-Agent, JSON bodies."""

    def __init__(self, base: str, key: str) -> None:
        self.base = base.rstrip("/")
        self.key = key

    def open(self, method: str, path: str, body: dict | None = None):
        # A named agent: some hosts refuse urllib's default one.
        headers = {"Authorization": f"Bearer {self.key}", "User-Agent": USER_AGENT}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"

        return send(urllib.request.Request(self.base + path, data=data, method=method, headers=headers))

    def retrying(self, method: str, path: str, body: dict | None = None):
        """open(), retrying a 503 that sends Retry-After: the API sends it only where a repeat is safe."""
        retries = 0
        while True:
            try:
                return self.open(method, path, body)
            except ApiError as err:
                if err.status != HTTP_UNAVAILABLE or err.retry_after is None:
                    raise
                if retries == MAX_UNAVAILABLE_RETRIES:
                    raise
                retries += 1
                log(f"unavailable (HTTP 503), retrying in {err.retry_after}s ({retries}/{MAX_UNAVAILABLE_RETRIES})")
                time.sleep(err.retry_after)


def scene_path(scene_id: str) -> str:
    return "/v1/scenes/" + quote(scene_id, safe="")


def resume_command(args: argparse.Namespace, scene_id: str, fmt: str) -> str:
    """The command that picks this scene up again: poll, then download.

    args.runner names the script, so another script that reuses these steps
    prints its own command.
    """
    command = f"{args.runner} --scene-id {scene_id} --format {fmt}"
    if args.out != DEFAULT_OUT:
        command += f" --out {shlex.quote(args.out)}"
    return f"resume: {command}"


def create(api: Api, file: Path, content_type: str, preset: str) -> tuple[str, str]:
    """POST /v1/scenes. Never retried: a repeat makes a second scene."""
    body = {
        "title": file.name[:TITLE_MAX_CHARS],
        "content_type": content_type,
        "file_size": file.stat().st_size,
        "preset": preset,
    }
    try:
        data = read_data(api.open("POST", "/v1/scenes", body))
    except ApiError as err:
        if err.status != HTTP_UNAVAILABLE:
            raise
        fail(err, "hint: create is not safe to repeat; check GET /v1/scenes for this scene before creating again")

    log(f"created scene {data['scene_id']}")
    return data["scene_id"], data["upload_url"]


def upload(url: str, file: Path, content_type: str) -> None:
    """PUT the file to storage, streamed from disk.

    The URL signs the Content-Type, so it must match create's. An explicit
    Content-Length stops urllib sending the file chunked, which storage
    refuses. No API key: the URL is the credential.
    """
    size = file.stat().st_size
    headers = {"Content-Type": content_type, "Content-Length": str(size), "User-Agent": USER_AGENT}
    with file.open("rb") as body:
        try:
            send(urllib.request.Request(url, data=body, method="PUT", headers=headers)).close()
        except ApiError as err:
            fail(err, "hint: the upload to storage failed; its URL lasts 24 hours")

    log(f"uploaded {size} bytes")


def start(api: Api, scene_id: str, args: argparse.Namespace) -> None:
    """POST /process with `{}`, the same body every time, so a repeat replays the launch.

    queue_full charges nothing: wait Retry-After and send it again, for up
    to MAX_QUEUE_WAIT_MINUTES. insufficient_credits charges nothing either:
    the scene waits, uploaded, for --scene-id once credits are added.
    """
    deadline = time.monotonic() + MAX_QUEUE_WAIT_MINUTES * SECONDS_PER_MINUTE
    while True:
        try:
            read_data(api.retrying("POST", f"{scene_path(scene_id)}/process", {}))
            log("processing started")
            return
        except ApiError as err:
            if err.code == "insufficient_credits":
                fail(err, f"hint: add credits at {BILLING_PAGE}", resume_command(args, scene_id, args.format))
            if err.code != "queue_full" or err.retry_after is None:
                raise
            if time.monotonic() + err.retry_after > deadline:
                fail(err, f"hint: the wait for a free slot would pass {MAX_QUEUE_WAIT_MINUTES} minutes; nothing was charged",
                     resume_command(args, scene_id, args.format))
            log(f"queue full: {err.message} Waiting {err.retry_after}s.")
            time.sleep(err.retry_after)


def progress(scene: dict) -> str:
    """`processing: training 42%` while a stage runs, then e.g. `status: complete`.

    Only a running scene has a stage and a pct. Its status is the stored
    stage; processing_stage can be fresher.
    """
    status = scene["status"]
    if status in IDLE_STATUSES:
        return f"status: {status}"

    line = f"processing: {scene.get('processing_stage') or status}"
    if scene.get("processing_pct") is not None:
        line += f" {math.floor(scene['processing_pct'])}%"
    return line


def wait(api: Api, scene_id: str, args: argparse.Namespace) -> dict:
    """Poll until the scene is complete, failed or cancelled. Any other status is still working."""
    deadline = time.monotonic() + MAX_WAIT_MINUTES * SECONDS_PER_MINUTE
    shown = None
    while True:
        scene = read_data(api.retrying("GET", scene_path(scene_id)))

        # Print only what changed.
        line = progress(scene)
        if line != shown:
            log(line)
            shown = line

        if scene["status"] in TERMINAL_STATUSES:
            return scene

        if time.monotonic() > deadline:
            log(f"error: scene {scene_id} is still {scene['status']} after {MAX_WAIT_MINUTES} minutes")
            log(resume_command(args, scene_id, args.format))
            sys.exit(EXIT_FAILED)

        time.sleep(args.poll_seconds)


def download(api: Api, scene_id: str, args: argparse.Namespace) -> None:
    """GET /download?format=..., streamed to <out>/scene-<id>.<format sent>."""
    try:
        response = api.retrying("GET", f"{scene_path(scene_id)}/download?format={args.format}")
    except ApiError as err:
        if err.status != HTTP_NOT_FOUND or args.format != "spz":
            raise
        fail(err, "hint: this scene has no SPZ file; try --format sog", resume_command(args, scene_id, "sog"))

    with response:
        # sog and ply fall back to each other: name the file for what came.
        sent = response.headers.get("X-Splat-Format")
        if sent not in FORMATS:
            sent = args.format

        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        target = out / f"scene-{scene_id}.{sent}"
        with target.open("wb") as file:
            shutil.copyfileobj(response, file)

    print(f"saved {target} ({target.stat().st_size} bytes, {sent})", flush=True)


def launch(api: Api, args: argparse.Namespace) -> str:
    """Create, upload and start a scene from args.file, or pick up args.scene_id. Returns the scene id."""
    if args.scene_id:
        # Uploaded but never started (e.g. queue_full ran out): start it.
        if read_data(api.retrying("GET", scene_path(args.scene_id)))["status"] == UPLOADING:
            start(api, args.scene_id, args)
        return args.scene_id

    file = Path(args.file)
    content_type = CONTENT_TYPES[file.suffix.lower()]
    scene_id, upload_url = create(api, file, content_type, args.preset)
    upload(upload_url, file, content_type)
    start(api, scene_id, args)
    return scene_id


def finish(api: Api, scene_id: str, args: argparse.Namespace) -> dict | None:
    """Wait for the scene, then download it. Returns the complete scene, or None when it failed or was cancelled."""
    scene = wait(api, scene_id, args)
    if scene["status"] == "failed":
        log(f"error: scene {scene_id} failed: {scene.get('processing_error') or NO_REASON}")
        log("hint: a failed scene is refunded automatically")
        return None
    if scene["status"] == "cancelled":
        log(f"error: scene {scene_id} was cancelled: {scene.get('processing_error') or NO_REASON}")
        return None

    download(api, scene_id, args)
    if scene.get("viewer_url"):
        print(f"viewer: {scene['viewer_url']}", flush=True)
    return scene


def run(api: Api, args: argparse.Namespace) -> int:
    scene = finish(api, launch(api, args), args)
    return EXIT_OK if scene else EXIT_FAILED


def read_env(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Add the environment's settings to args: key, API base and poll interval. A bad one is a usage error."""
    try:
        args.poll_seconds = float(os.environ.get("SPLAT3D_POLL_SECONDS") or DEFAULT_POLL_SECONDS)
    except ValueError:
        parser.error("SPLAT3D_POLL_SECONDS must be a number")

    args.key = os.environ.get("SPLAT3D_API_KEY")
    if not args.key:
        parser.error(f"set SPLAT3D_API_KEY; create a key at {KEYS_PAGE}")

    args.api_base = os.environ.get("SPLAT3D_API_BASE") or DEFAULT_API_BASE


def guard(body: Callable[[Api, argparse.Namespace], int], api: Api, args: argparse.Namespace) -> int:
    """Run body(api, args) and turn an API refusal, a network failure or Ctrl-C into exit 1."""
    try:
        return body(api, args)
    except ApiError as err:
        err.report()
        if err.code in HINTS:
            log(f"hint: {HINTS[err.code]}")
        return EXIT_FAILED
    except urllib.error.URLError as err:
        log(f"error: network: {err.reason}")
        return EXIT_FAILED
    except KeyboardInterrupt:
        log("stopped")
        return EXIT_FAILED


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Turn a video or a zip of photos into a 3D scene with the Splat3D API.",
        epilog="Environment: SPLAT3D_API_KEY (required), SPLAT3D_API_BASE, SPLAT3D_POLL_SECONDS.",
    )
    parser.add_argument("file", nargs="?", help=".mp4, .mov or .zip of photos")
    parser.add_argument("--preset", choices=PRESETS, default="fast")
    parser.add_argument("--format", choices=FORMATS, default="spz")
    parser.add_argument("--out", default=DEFAULT_OUT, help="directory for the model (default: .)")
    parser.add_argument("--scene-id", help="resume a scene instead of creating one")
    args = parser.parse_args()

    # Usage errors exit 2 before any request.
    if bool(args.file) == bool(args.scene_id):
        parser.error("give a FILE or --scene-id, not both")
    if args.file and not Path(args.file).is_file():
        parser.error(f"no such file: {args.file}")
    if args.file and Path(args.file).suffix.lower() not in CONTENT_TYPES:
        parser.error(f"unsupported file type {Path(args.file).suffix or '(none)'}: use .mp4, .mov or .zip")

    read_env(parser, args)
    args.runner = RUNNER
    return args


def main() -> int:
    args = parse_args()
    return guard(run, Api(args.api_base, args.key), args)


if __name__ == "__main__":
    sys.exit(main())
