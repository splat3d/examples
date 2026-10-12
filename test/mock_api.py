#!/usr/bin/env python3
"""A stand-in for the Splat3D API and its upload storage, for the tests.

It answers the five calls the quickstarts make, checks each request the way
the real service does, and logs every call so a test can count them:

    POST /v1/scenes                    create  -> 201 {sceneId, uploadUrl}
    PUT  /upload/<id>                  storage -> 200 (the presigned upload)
    POST /v1/scenes/<id>/process       launch  -> 200 {status: "processing"}
    GET  /v1/scenes/<id>               poll    -> extracting_frames, training, complete
    GET  /v1/scenes/<id>/download      model   -> 200 bytes + X-Splat-Format

A scenario picks one deviation from that happy path (SCENARIOS below).

Run it on its own to try an example without a key:

    python3 test/mock_api.py --scenario queue_full_once --port 8787
    SPLAT3D_API_KEY=s3d_test_key SPLAT3D_API_BASE=http://127.0.0.1:8787 \\
        python3 python/quickstart.py capture.mp4
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

API_KEY = "s3d_test_key"
SCENE_ID = "a1b2c3d4e5f6"
VIEWER_URL = f"https://splat-3d.com/tour/{SCENE_ID}"

# What each format's download sends. An SPZ (version 4) starts with "NGSP".
MODEL_BYTES = {
    "spz": b"NGSP" + b"mock spz model",
    "sog": b"PK\x03\x04" + b"mock sog model",
    "ply": b"ply\nmock ply model\n",
}

# Polls after the launch, in order; the last one repeats. `status` is the
# stored value: a running scene's status is its pipeline stage. Stage and pct
# are null unless the scene is running.
LAUNCHED_STATUS = "extracting_frames"
PROGRESSION = [
    {"status": LAUNCHED_STATUS, "processing_stage": LAUNCHED_STATUS, "processing_pct": 5},
    {"status": "training", "processing_stage": "training", "processing_pct": 40, "preview_ready": True},
    {"status": "complete", "processing_stage": None, "processing_pct": None, "preview_ready": True},
]
FAILED_STEP = {
    "status": "failed",
    "processing_stage": None,
    "processing_pct": None,
    "processing_error": "Not enough overlap between frames to reconstruct the scene.",
}

SCENARIOS = (
    "happy",
    "queue_full_once",       # first /process: 429 queue_full, Retry-After 1
    "queue_full_long",       # every /process: 429 queue_full, Retry-After past the wait cap
    "quota_exceeded",        # /process: 429 quota_exceeded, no Retry-After
    "source_unusable",       # /process: 422 with details
    "unavailable_once",      # first GET of the scene: 503, Retry-After 1
    "create_unavailable",    # create: 503, no Retry-After (create is not retry-safe)
    "create_invalid",        # create: 400 invalid_input, one {field, message} per problem
    "insufficient_credits",  # /process: 402
    "payload_too_large",     # /process: 413, the upload is over the cap
    "failed",                # the scene ends failed
    "no_spz",                # format=spz: 404
    "format_fallback",       # format=ply: the server sends sog
    "resume",                # the scene already exists, complete
    "resume_uploading",      # the scene already exists, uploaded but never launched
    "no_holdout",            # the scene completes with no held-out metrics, as the fast preset does
)

# A complete scene's metrics. Made up: tests only check they are printed.
METRICS = {"ssim": 0.8765, "psnr_holdout": 12.34, "ssim_holdout": 0.5678}

# The real API's values (Retry-After for queue_full is 120 s; the mock's are
# short so the tests run fast).
QUEUE_FULL_RETRY_AFTER = "1"
QUEUE_FULL_LONG_RETRY_AFTER = "3600"
UNAVAILABLE_RETRY_AFTER = "1"

# Cloudflare's bot check (error 1010) answers urllib's default agent so on some hosts.
BLOCKED_AGENT_PREFIX = "Python-urllib/"
BLOCKED_AGENT_BODY = b"error code: 1010"

ACCEPTED_CONTENT_TYPES = ("application/zip", "application/x-zip-compressed", "application/x-r3d")
JSON_TYPE = re.compile(r"^application/json(;.*)?$")

SCENE_PATH = re.compile(r"^/v1/scenes/([0-9a-f]{12})$")
PROCESS_PATH = re.compile(r"^/v1/scenes/([0-9a-f]{12})/process$")
DOWNLOAD_PATH = re.compile(r"^/v1/scenes/([0-9a-f]{12})/download$")
UPLOAD_PATH = re.compile(r"^/upload/([0-9a-f]{12})$")


@dataclass
class Call:
    """One request as the mock received it. Header names are lowercase."""

    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: bytes


@dataclass
class Scene:
    status: str
    content_type: str | None = None
    launched: bool = False
    polls: int = 0


@dataclass
class State:
    scenario: str
    calls: list[Call] = field(default_factory=list)
    upload_problems: list[str] = field(default_factory=list)
    scenes: dict[str, Scene] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.1 so `Expect: 100-continue` (curl -T on big files) is answered.
    protocol_version = "HTTP/1.1"
    state: State  # set on the subclass MockApi makes

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # Quiet: the tests read the call log, not stderr.
        return

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def do_PUT(self) -> None:
        self._handle()

    # -- dispatch ---------------------------------------------------------

    def _handle(self) -> None:
        url = urlsplit(self.path)
        headers = {k.lower(): v for k, v in self.headers.items()}
        body = self._read_body(headers)
        call = Call(self.command, url.path, parse_qs(url.query), headers, body)
        with self.state.lock:
            self.state.calls.append(call)
            self._route(call)

    def _read_body(self, headers: dict[str, str]) -> bytes:
        # A chunked body is refused on upload; read nothing so it shows.
        length = int(headers.get("content-length", "0") or "0")
        return self.rfile.read(length) if length else b""

    def _route(self, call: Call) -> None:
        if call.headers.get("user-agent", "").startswith(BLOCKED_AGENT_PREFIX):
            self._send(403, BLOCKED_AGENT_BODY, "text/plain")
            return

        upload = UPLOAD_PATH.match(call.path)
        if upload and call.method == "PUT":
            self._upload(call, upload.group(1))
            return

        if call.headers.get("authorization") != f"Bearer {API_KEY}":
            self._error(401, "unauthorized", "Invalid or expired API key.")
            return

        if call.path == "/v1/scenes" and call.method == "POST":
            self._create(call)
            return

        routes = (
            (PROCESS_PATH, "POST", self._process),
            (SCENE_PATH, "GET", self._get),
            (DOWNLOAD_PATH, "GET", self._download),
        )
        for pattern, method, handler in routes:
            match = pattern.match(call.path)
            if match and call.method == method:
                handler(call, match.group(1))
                return

        self._error(404, "not_found", "Not found.")

    # -- endpoints ---------------------------------------------------------

    def _create(self, call: Call) -> None:
        if self.state.scenario == "create_unavailable":
            self._error(503, "service_unavailable", "Unable to create the scene right now.")
            return

        if not JSON_TYPE.match(call.headers.get("content-type", "")):
            self._error(415, "invalid_input", "A request body must be JSON.")
            return

        body = self._json_body(call)
        if body is None:
            return

        # A schema failure lists each problem as {field, message}.
        if self.state.scenario == "create_invalid":
            problem = "Invalid enum value. Expected 'fast' | 'standard' | 'quality' | 'ultra' | 'custom', received 'nope'"
            self._error(400, "invalid_input", f"preset: {problem}", details=[{"field": "preset", "message": problem}])
            return

        content_type = body.get("content_type", "video/mp4")
        if not (content_type.startswith("video/") or content_type in ACCEPTED_CONTENT_TYPES):
            self._error(400, "invalid_input", "Please upload a video file, a zip of photos, or an R3D capture.")
            return

        self.state.scenes[SCENE_ID] = Scene("uploading", content_type)
        port = self.server.server_address[1]
        upload_url = f"http://127.0.0.1:{port}/upload/{SCENE_ID}?X-Amz-Signature=mock"
        self._json(201, {"data": {"sceneId": SCENE_ID, "uploadUrl": upload_url}})

    def _upload(self, call: Call, scene_id: str) -> None:
        # The checks storage makes of a presigned PUT, plus the ones that keep a
        # key out of storage logs.
        scene = self.state.scenes.get(scene_id)
        problems = []
        if scene is None:
            problems.append("upload for an unknown scene")
        elif call.headers.get("content-type") != scene.content_type:
            problems.append(f"content-type {call.headers.get('content-type')!r} != {scene.content_type!r}")
        if "content-length" not in call.headers:
            problems.append("no content-length")
        if "chunked" in call.headers.get("transfer-encoding", ""):
            problems.append("chunked transfer-encoding")
        if "authorization" in call.headers:
            problems.append("authorization header sent to storage")

        if problems:
            self.state.upload_problems.extend(problems)
            self._send(403, b"<Error><Code>SignatureDoesNotMatch</Code></Error>", "application/xml")
            return

        self._send(200, b"", "text/plain")

    def _process(self, call: Call, scene_id: str) -> None:
        content_type = call.headers.get("content-type", "")
        if call.body and not JSON_TYPE.match(content_type):
            self._error(415, "invalid_input", "A request body must be JSON.")
            return
        if JSON_TYPE.match(content_type) and self._json_body(call) is None:
            return

        scene = self.state.scenes.get(scene_id)
        if scene is None:
            self._error(404, "not_found", "Scene not found.")
            return

        scenario = self.state.scenario
        process_calls = sum(1 for c in self.state.calls if PROCESS_PATH.match(c.path))
        if scenario == "queue_full_once" and process_calls == 1:
            self._queue_full(QUEUE_FULL_RETRY_AFTER)
            return
        if scenario == "queue_full_long":
            self._queue_full(QUEUE_FULL_LONG_RETRY_AFTER)
            return
        if scenario == "quota_exceeded":
            self._error(429, "quota_exceeded", "Monthly scene processing limit reached.",
                        meta={"period": "2026-10", "limit": 5, "field": "scenes_processed"})
            return
        if scenario == "insufficient_credits":
            self._error(402, "insufficient_credits", "Insufficient credits. Need 10 credits to process this scene.")
            return
        if scenario == "payload_too_large":
            self._error(413, "payload_too_large", "Uploaded file is 6.0 GB, which exceeds the 5.0 GB upload limit.")
            return
        if scenario == "source_unusable":
            details = [{"field": "photo_count", "value": 3, "allowed": {"min": 5}, "hint": "Add JPEG or PNG photos."}]
            self._error(422, "source_unusable",
                        "The zip has 3 JPEG or PNG photos; a scene needs at least 5. Add photos and upload again. "
                        "Nothing was charged.", details=details)
            return

        # The launch stores the first stage. The answer's "processing" only says
        # the launch was accepted.
        scene.launched = True
        scene.status = LAUNCHED_STATUS
        self._json(200, {"data": {"status": "processing", "sceneId": scene_id, "message": "Processing started."}})

    def _get(self, call: Call, scene_id: str) -> None:
        scene = self.state.scenes.get(scene_id)
        if scene is None:
            self._error(404, "not_found", "Scene not found.")
            return

        scene_gets = sum(1 for c in self.state.calls if SCENE_PATH.match(c.path))
        if self.state.scenario == "unavailable_once" and scene_gets == 1:
            self._error(503, "service_unavailable", "Unable to read the scene right now.",
                        headers={"Retry-After": UNAVAILABLE_RETRY_AFTER})
            return

        if scene.launched:
            step = PROGRESSION[min(scene.polls, len(PROGRESSION) - 1)]
            if self.state.scenario == "failed" and scene.polls >= len(PROGRESSION) - 1:
                step = FAILED_STEP
            scene.polls += 1
            scene.status = step["status"]
        else:
            step = {"status": scene.status}

        base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        detail = scene_detail(scene_id, step, base_url)
        if step["status"] == "complete":
            detail.update(METRICS)
        if self.state.scenario == "no_holdout":
            detail.update(psnr_holdout=None, ssim_holdout=None)
        self._json(200, {"data": detail})

    def _download(self, call: Call, scene_id: str) -> None:
        if scene_id not in self.state.scenes:
            self._error(404, "not_found", "Scene not found.")
            return

        wanted = call.query.get("format", ["sog"])[0]
        if wanted == "spz" and self.state.scenario == "no_spz":
            self._error(404, "not_found", "No SPZ file available for this scene.")
            return

        sent = "sog" if self.state.scenario == "format_fallback" else wanted
        self._send(200, MODEL_BYTES[sent], "application/octet-stream", {"X-Splat-Format": sent})

    # -- requests ----------------------------------------------------------

    def _json_body(self, call: Call) -> dict | None:
        """The parsed JSON body. A body that doesn't parse, empty included, answers 400 and returns None."""
        try:
            return json.loads(call.body)
        except ValueError:
            self._error(400, "invalid_input", "Malformed JSON in request body")
            return None

    # -- responses ---------------------------------------------------------

    def _queue_full(self, retry_after: str) -> None:
        self._error(429, "queue_full",
                    "You have 2 scenes processing, and your plan allows 2 at once. Try again when one finishes.",
                    meta={"limit": 2, "current": 2}, headers={"Retry-After": retry_after})

    def _error(self, status: int, code: str, message: str, *, details: list | None = None,
               meta: dict | None = None, headers: dict[str, str] | None = None) -> None:
        error: dict = {"code": code, "message": message}
        if details:
            error["details"] = details
        self._json(status, {"error": error, "meta": dict(meta or {})}, headers)

    def _json(self, status: int, payload: dict, headers: dict[str, str] | None = None) -> None:
        # Every envelope carries the request id, as the real one does.
        request_id = str(uuid.uuid4())
        payload.setdefault("meta", {})["request_id"] = request_id
        extra = {"X-Request-Id": request_id, **(headers or {})}
        self._send(status, json.dumps(payload).encode(), "application/json", extra)

    def _send(self, status: int, body: bytes, content_type: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


def scene_detail(scene_id: str, step: dict, base_url: str) -> dict:
    """The flat SceneDetail that GET, the list and PATCH return: every key, null when it doesn't apply."""
    complete = step["status"] == "complete"
    scene_url = f"{base_url}/v1/scenes/{scene_id}"
    return {
        "id": scene_id,
        "title": "capture",
        "description": None,
        "address": None,
        "status": step["status"],
        "training_model": "gsplat",
        "is_public": False,
        "thumbnail_r2_key": None,
        "thumbnail_url": None,
        "preview_ready": step.get("preview_ready", False),
        "num_gaussians": 123456 if complete else None,
        "ssim": None,
        "psnr_holdout": None,
        "ssim_holdout": None,
        "processing_stage": step.get("processing_stage"),
        "processing_pct": step.get("processing_pct"),
        "processing_error": step.get("processing_error"),
        "viewer_url": VIEWER_URL if complete else None,
        "download_url": f"{scene_url}/download" if complete else None,
        "format": "sog" if complete else None,
        "lod_meta_url": None,
        "created_at": "2026-10-11T12:00:00.000Z",
        "updated_at": "2026-10-11T12:05:00.000Z",
    }


class MockApi:
    """The mock on 127.0.0.1, in a thread. Use as a context manager."""

    def __init__(self, scenario: str = "happy", port: int = 0) -> None:
        if scenario not in SCENARIOS:
            raise ValueError(f"unknown scenario {scenario!r}")

        self.state = State(scenario)
        if scenario == "resume":
            self.state.scenes[SCENE_ID] = Scene("complete")
        if scenario == "resume_uploading":
            self.state.scenes[SCENE_ID] = Scene("uploading", "video/mp4")

        handler = type("BoundHandler", (Handler,), {"state": self.state})
        self.server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    @property
    def calls(self) -> list[Call]:
        with self.state.lock:
            return list(self.state.calls)

    @property
    def upload_problems(self) -> list[str]:
        with self.state.lock:
            return list(self.state.upload_problems)

    def count(self, method: str, pattern: str) -> int:
        """How many calls matched a method and a full-path regex."""
        return sum(1 for c in self.calls if c.method == method and re.fullmatch(pattern, c.path))

    def __enter__(self) -> MockApi:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", choices=SCENARIOS, default="happy")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()

    with MockApi(args.scenario, args.port) as mock:
        print(f"mock Splat3D API ({args.scenario}) on {mock.base_url}, key {API_KEY}")
        try:
            mock.thread.join()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
