"""Run each quickstart against the mock API and check what it sent and saved.

    python3 -m unittest discover -s test -v

Every test runs once per language. A language whose runtime is missing is
skipped (Node below 22; bash, curl or jq absent). EXAMPLES_BASH picks the bash
the curl example runs under, e.g. /bin/bash for macOS's 3.2.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mock_api import API_KEY, MODEL_BYTES, SCENE_ID, VIEWER_URL, MockApi

ROOT = Path(__file__).resolve().parent.parent
TIMEOUT_SECONDS = 60
POLL_SECONDS = "0.05"
CAPTURE_BYTES = bytes(range(256)) * 8
MIN_NODE_MAJOR = 22
USER_AGENT_PREFIX = "splat3d-examples/"
BILLING_PAGE = "https://splat-3d.com/dashboard/billing"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

# Full-path regexes for MockApi.count.
CREATE = r"/v1/scenes"
SCENE = rf"/v1/scenes/{SCENE_ID}"
PROCESS = rf"{SCENE}/process"
DOWNLOAD = rf"{SCENE}/download"
UPLOAD = rf"/upload/{SCENE_ID}"


def node_major() -> int:
    """Node's major version, or 0 when there is no node."""
    if not shutil.which("node"):
        return 0
    version = subprocess.run(["node", "--version"], capture_output=True, text=True).stdout
    return int(version.lstrip("v").split(".")[0])


BASH = os.environ.get("EXAMPLES_BASH") or shutil.which("bash")
HAS_CURL_TOOLS = bool(BASH and shutil.which("curl") and shutil.which("jq"))


class Contract:
    """The README's contract, checked for one example. Subclasses set `command`."""

    command: list[str]

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="splat3d-examples-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.capture = self.write_capture("capture.mp4")
        self.out = self.tmp / "out"

    def write_capture(self, name: str) -> Path:
        path = self.tmp / name
        path.write_bytes(CAPTURE_BYTES)
        return path

    def run_example(self, scenario: str, *args: str, key: str | None = API_KEY) -> subprocess.CompletedProcess:
        """Run the example against a fresh mock, from the temp dir; keep the mock as self.mock."""
        with MockApi(scenario) as mock:
            env = {
                **os.environ,
                "SPLAT3D_API_BASE": mock.base_url,
                "SPLAT3D_POLL_SECONDS": POLL_SECONDS,
                "NO_PROXY": "127.0.0.1",
                "no_proxy": "127.0.0.1",
            }
            env.pop("SPLAT3D_API_KEY", None)
            if key is not None:
                env["SPLAT3D_API_KEY"] = key

            result = subprocess.run(
                [*self.command, *args], env=env, cwd=self.tmp,
                capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
            )
        self.mock = mock
        return result

    def assertExit(self, result: subprocess.CompletedProcess, code: int) -> None:
        self.assertEqual(result.returncode, code, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")

    def calls(self, method: str, path: str) -> list:
        return [c for c in self.mock.calls if c.method == method and c.path == path]

    # -- the happy path ------------------------------------------------------

    def test_happy_path(self) -> None:
        result = self.run_example("happy", "capture.mp4")

        self.assertExit(result, EXIT_OK)
        saved = self.tmp / f"scene-{SCENE_ID}.spz"
        self.assertEqual(saved.read_bytes(), MODEL_BYTES["spz"])
        size = len(MODEL_BYTES["spz"])
        self.assertRegex(result.stdout, rf"saved (\./)?{saved.name} \({size} bytes, spz\)")
        self.assertIn(VIEWER_URL, result.stdout)

        self.assertEqual(self.mock.count("POST", CREATE), 1)
        self.assertEqual(self.mock.count("PUT", UPLOAD), 1)
        self.assertEqual(self.mock.count("POST", PROCESS), 1)
        self.assertEqual(self.mock.count("GET", SCENE), 3)
        self.assertEqual(self.mock.count("GET", DOWNLOAD), 1)

    def test_progress(self) -> None:
        # A running scene's status is its stage: print stage and pct, once
        # each, then the final status alone.
        result = self.run_example("happy", str(self.capture), "--out", str(self.out))

        self.assertExit(result, EXIT_OK)
        progress = [line for line in result.stderr.splitlines() if line.startswith(("processing:", "status:"))]
        self.assertEqual(progress, [
            "processing: extracting_frames 5%",
            "processing: training 40%",
            "status: complete",
        ])

    def test_create_body(self) -> None:
        self.run_example("happy", str(self.capture), "--out", str(self.out))

        create = self.calls("POST", CREATE)[0]
        self.assertEqual(create.headers["content-type"], "application/json")
        self.assertEqual(json.loads(create.body), {
            "title": "capture.mp4",
            "content_type": "video/mp4",
            "file_size": len(CAPTURE_BYTES),
            "preset": "fast",
        })

    def test_upload_request(self) -> None:
        self.run_example("happy", str(self.capture), "--out", str(self.out))

        self.assertEqual(self.mock.upload_problems, [])
        put = self.calls("PUT", UPLOAD)[0]
        self.assertEqual(put.headers["content-type"], "video/mp4")
        self.assertEqual(put.headers["content-length"], str(len(CAPTURE_BYTES)))
        self.assertNotIn("transfer-encoding", put.headers)
        self.assertNotIn("authorization", put.headers)
        self.assertEqual(put.body, CAPTURE_BYTES)

    def test_process_body(self) -> None:
        self.run_example("happy", str(self.capture), "--out", str(self.out))

        process = self.calls("POST", PROCESS)[0]
        self.assertTrue(process.headers["content-type"].startswith("application/json"))
        self.assertEqual(json.loads(process.body), {})

    def test_api_calls_named(self) -> None:
        self.run_example("happy", str(self.capture), "--out", str(self.out))

        api_calls = [c for c in self.mock.calls if c.path.startswith("/v1/")]
        self.assertTrue(api_calls)
        for call in api_calls:
            self.assertEqual(call.headers.get("authorization"), f"Bearer {API_KEY}", call.path)
            self.assertTrue(call.headers.get("user-agent", "").startswith(USER_AGENT_PREFIX), call.headers)

    def test_download_format(self) -> None:
        result = self.run_example("happy", str(self.capture), "--out", str(self.out), "--format", "sog")

        self.assertExit(result, EXIT_OK)
        self.assertEqual(self.calls("GET", DOWNLOAD)[0].query, {"format": ["sog"]})
        self.assertEqual((self.out / f"scene-{SCENE_ID}.sog").read_bytes(), MODEL_BYTES["sog"])

    def test_content_types(self) -> None:
        for name, content_type in (("walk.MOV", "video/quicktime"), ("photos.zip", "application/zip")):
            with self.subTest(name=name):
                result = self.run_example("happy", str(self.write_capture(name)), "--out", str(self.out))

                self.assertExit(result, EXIT_OK)
                self.assertEqual(json.loads(self.calls("POST", CREATE)[0].body)["content_type"], content_type)
                self.assertEqual(self.calls("PUT", UPLOAD)[0].headers["content-type"], content_type)

    # -- refusals and retries ------------------------------------------------

    def test_queue_full_waits(self) -> None:
        result = self.run_example("queue_full_once", str(self.capture), "--out", str(self.out))

        self.assertExit(result, EXIT_OK)
        self.assertIn("queue full", result.stderr)
        processes = self.calls("POST", PROCESS)
        self.assertEqual(len(processes), 2)
        self.assertEqual(processes[0].body, processes[1].body)
        self.assertTrue((self.out / f"scene-{SCENE_ID}.spz").exists())

    def test_queue_full_gives_up(self) -> None:
        result = self.run_example("queue_full_long", str(self.capture), "--out", str(self.out))

        self.assertExit(result, EXIT_FAILED)
        self.assertIn("queue_full", result.stderr)
        self.assertIn(f"--scene-id {SCENE_ID}", result.stderr)
        self.assertEqual(self.mock.count("POST", PROCESS), 1)
        self.assertEqual(self.mock.count("GET", SCENE), 0)

    def test_quota_exceeded(self) -> None:
        result = self.run_example("quota_exceeded", str(self.capture), "--out", str(self.out))

        self.assertExit(result, EXIT_FAILED)
        self.assertIn("HTTP 429 quota_exceeded", result.stderr)
        self.assertEqual(self.mock.count("POST", PROCESS), 1)

    def test_source_unusable(self) -> None:
        result = self.run_example("source_unusable", str(self.capture), "--out", str(self.out))

        self.assertExit(result, EXIT_FAILED)
        self.assertIn("HTTP 422 source_unusable", result.stderr)
        self.assertIn('photo_count: 3 (allowed {"min":5}) Add JPEG or PNG photos.', result.stderr)
        self.assertRegex(result.stderr, r"\(request [0-9a-f-]{36}\)")
        self.assertIn("hint: run again with a usable file", result.stderr)
        self.assertEqual(self.mock.count("POST", PROCESS), 1)
        self.assertEqual(self.mock.count("GET", SCENE), 0)

    def test_insufficient_credits(self) -> None:
        result = self.run_example("insufficient_credits", str(self.capture), "--out", str(self.out))

        self.assertExit(result, EXIT_FAILED)
        self.assertIn("HTTP 402 insufficient_credits: Insufficient credits.", result.stderr)
        self.assertIn(f"hint: add credits at {BILLING_PAGE}", result.stderr)
        self.assertIn(f"--scene-id {SCENE_ID}", result.stderr)
        self.assertEqual(self.mock.count("POST", PROCESS), 1)
        self.assertEqual(self.mock.count("GET", SCENE), 0)

    def test_payload_too_large(self) -> None:
        result = self.run_example("payload_too_large", str(self.capture), "--out", str(self.out))

        self.assertExit(result, EXIT_FAILED)
        self.assertIn("HTTP 413 payload_too_large: Uploaded file is 6.0 GB", result.stderr)
        self.assertIn("hint: the upload limit is 5 GB", result.stderr)
        self.assertEqual(self.mock.count("GET", SCENE), 0)

    def test_invalid_input_details(self) -> None:
        # A schema failure's details are {field, message}, not {field, value, allowed}.
        result = self.run_example("create_invalid", str(self.capture), "--out", str(self.out))

        self.assertExit(result, EXIT_FAILED)
        self.assertIn("HTTP 400 invalid_input: preset: Invalid enum value.", result.stderr)
        self.assertIn("\n  preset: Invalid enum value.", result.stderr)
        self.assertNotIn("allowed", result.stderr)
        self.assertEqual(self.mock.count("PUT", UPLOAD), 0)

    def test_unavailable_retries(self) -> None:
        result = self.run_example("unavailable_once", str(self.capture), "--out", str(self.out))

        self.assertExit(result, EXIT_OK)
        self.assertIn("unavailable (HTTP 503), retrying in 1s (1/5)", result.stderr)
        self.assertTrue((self.out / f"scene-{SCENE_ID}.spz").exists())

    def test_create_unavailable(self) -> None:
        result = self.run_example("create_unavailable", str(self.capture), "--out", str(self.out))

        self.assertExit(result, EXIT_FAILED)
        self.assertIn("HTTP 503 service_unavailable", result.stderr)
        self.assertIn("GET /v1/scenes", result.stderr)
        self.assertEqual(self.mock.count("POST", CREATE), 1)
        self.assertEqual(self.mock.count("PUT", UPLOAD), 0)

    def test_failed_scene(self) -> None:
        result = self.run_example("failed", str(self.capture), "--out", str(self.out))

        self.assertExit(result, EXIT_FAILED)
        self.assertIn(f"scene {SCENE_ID} failed: Not enough overlap", result.stderr)
        self.assertIn("refunded", result.stderr)
        self.assertEqual(self.mock.count("GET", DOWNLOAD), 0)

    def test_no_spz(self) -> None:
        result = self.run_example("no_spz", str(self.capture), "--out", str(self.out))

        self.assertExit(result, EXIT_FAILED)
        self.assertIn("--format sog", result.stderr)
        self.assertFalse((self.out / f"scene-{SCENE_ID}.spz").exists())

    def test_format_fallback(self) -> None:
        result = self.run_example("format_fallback", str(self.capture), "--out", str(self.out), "--format", "ply")

        self.assertExit(result, EXIT_OK)
        self.assertEqual(self.calls("GET", DOWNLOAD)[0].query, {"format": ["ply"]})
        self.assertEqual((self.out / f"scene-{SCENE_ID}.sog").read_bytes(), MODEL_BYTES["sog"])
        self.assertFalse((self.out / f"scene-{SCENE_ID}.ply").exists())

    def test_bad_key(self) -> None:
        result = self.run_example("happy", str(self.capture), "--out", str(self.out), key="s3d_wrong_key")

        self.assertExit(result, EXIT_FAILED)
        self.assertIn("HTTP 401 unauthorized", result.stderr)
        self.assertIn("SPLAT3D_API_KEY", result.stderr)

    # -- resume ---------------------------------------------------------------

    def test_resume(self) -> None:
        result = self.run_example("resume", "--scene-id", SCENE_ID, "--out", str(self.out))

        self.assertExit(result, EXIT_OK)
        self.assertEqual(self.mock.count("POST", CREATE), 0)
        self.assertEqual(self.mock.count("PUT", UPLOAD), 0)
        self.assertEqual(self.mock.count("POST", PROCESS), 0)
        self.assertEqual((self.out / f"scene-{SCENE_ID}.spz").read_bytes(), MODEL_BYTES["spz"])

    def test_resume_starts_uploaded(self) -> None:
        result = self.run_example("resume_uploading", "--scene-id", SCENE_ID, "--out", str(self.out))

        self.assertExit(result, EXIT_OK)
        self.assertEqual(self.mock.count("POST", CREATE), 0)
        self.assertEqual(self.mock.count("PUT", UPLOAD), 0)
        self.assertEqual(self.mock.count("POST", PROCESS), 1)
        self.assertTrue((self.out / f"scene-{SCENE_ID}.spz").exists())

    # -- usage errors: exit 2, no request -------------------------------------

    def test_missing_key(self) -> None:
        result = self.run_example("happy", str(self.capture), key=None)

        self.assertExit(result, EXIT_USAGE)
        self.assertIn("SPLAT3D_API_KEY", result.stderr)
        self.assertEqual(self.mock.calls, [])

    def test_unsupported_file(self) -> None:
        result = self.run_example("happy", str(self.write_capture("notes.txt")))

        self.assertExit(result, EXIT_USAGE)
        self.assertIn(".txt", result.stderr)
        self.assertEqual(self.mock.calls, [])

    def test_no_file(self) -> None:
        result = self.run_example("happy")

        self.assertExit(result, EXIT_USAGE)
        self.assertEqual(self.mock.calls, [])

    def test_bad_format(self) -> None:
        result = self.run_example("happy", str(self.capture), "--format", "obj")

        self.assertExit(result, EXIT_USAGE)
        self.assertEqual(self.mock.calls, [])


class PythonQuickstart(Contract, unittest.TestCase):
    command = [sys.executable, str(ROOT / "python" / "quickstart.py")]


@unittest.skipUnless(node_major() >= MIN_NODE_MAJOR, f"needs Node {MIN_NODE_MAJOR}+")
class NodeQuickstart(Contract, unittest.TestCase):
    command = ["node", str(ROOT / "javascript" / "quickstart.mjs")]


@unittest.skipUnless(HAS_CURL_TOOLS, "needs bash, curl and jq")
class CurlQuickstart(Contract, unittest.TestCase):
    command = [BASH or "bash", str(ROOT / "curl" / "quickstart.sh")]


if __name__ == "__main__":
    unittest.main()
