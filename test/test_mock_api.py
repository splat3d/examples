"""Check the mock keeps the API 1.3.0 contract the examples are tested against.

    python3 -m unittest discover -s test -v
"""

from __future__ import annotations

import json
import unittest
import urllib.error
import urllib.request

from mock_api import API_KEY, SCENE_ID, MockApi

TIMEOUT_SECONDS = 10
USER_AGENT = "splat3d-examples-test"
JSON_TYPE = "application/json"
CREATE_BODY = {"content_type": "video/mp4", "file_size": 1, "preset": "fast"}

HTTP_OK = 200
HTTP_CREATED = 201
HTTP_BAD_REQUEST = 400

CREATE = "/v1/scenes"
SCENE = f"/v1/scenes/{SCENE_ID}"
PROCESS = f"{SCENE}/process"

# No proxy: the mock is on 127.0.0.1.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def send(mock: MockApi, method: str, path: str, body: bytes | None = None) -> tuple[int, dict]:
    """One API call with the test key; returns the status and the parsed envelope."""
    headers = {"Authorization": f"Bearer {API_KEY}", "User-Agent": USER_AGENT}
    if body is not None:
        headers["Content-Type"] = JSON_TYPE

    request = urllib.request.Request(mock.base_url + path, data=body, method=method, headers=headers)
    try:
        with OPENER.open(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as err:
        with err:
            return err.code, json.load(err)


def as_json(body: object) -> bytes:
    return json.dumps(body).encode()


class LaunchAnswers(unittest.TestCase):
    def test_create_sends_both_key_styles(self) -> None:
        with MockApi() as mock:
            status, envelope = send(mock, "POST", CREATE, as_json(CREATE_BODY))

        self.assertEqual(status, HTTP_CREATED)
        data = envelope["data"]
        self.assertEqual(data["scene_id"], SCENE_ID)
        self.assertEqual(data["id"], SCENE_ID)
        self.assertEqual(data["sceneId"], SCENE_ID)
        self.assertEqual(data["upload_url"], data["uploadUrl"])

    def test_process_sends_both_key_styles(self) -> None:
        with MockApi("resume_uploading") as mock:
            status, envelope = send(mock, "POST", PROCESS, b"{}")

        self.assertEqual(status, HTTP_OK)
        self.assertEqual(envelope["data"]["status"], "processing")
        self.assertEqual(envelope["data"]["scene_id"], SCENE_ID)
        self.assertEqual(envelope["data"]["id"], SCENE_ID)
        self.assertEqual(envelope["data"]["sceneId"], SCENE_ID)

    def test_snake_case_only(self) -> None:
        with MockApi("snake_case_only") as mock:
            _, created = send(mock, "POST", CREATE, as_json(CREATE_BODY))
            _, launched = send(mock, "POST", PROCESS, b"{}")

        self.assertEqual(set(created["data"]), {"scene_id", "id", "upload_url"})
        self.assertNotIn("sceneId", launched["data"])
        self.assertEqual(launched["data"]["scene_id"], SCENE_ID)


class StrictBodies(unittest.TestCase):
    def test_create_refuses_unknown_key(self) -> None:
        with MockApi() as mock:
            status, envelope = send(mock, "POST", CREATE, as_json({**CREATE_BODY, "presets": "ultra"}))
            scenes = dict(mock.state.scenes)

        self.assertEqual(status, HTTP_BAD_REQUEST)
        self.assertEqual(envelope["error"]["code"], "invalid_input")
        self.assertEqual(envelope["error"]["message"], "presets: unrecognized key")
        self.assertEqual(envelope["error"]["details"], [{"field": "presets", "message": "unrecognized key"}])
        self.assertEqual(scenes, {})

    def test_create_names_each_unknown_key(self) -> None:
        with MockApi() as mock:
            _, envelope = send(mock, "POST", CREATE, as_json({"presets": "ultra", **CREATE_BODY, "fps": 2}))

        self.assertEqual([detail["field"] for detail in envelope["error"]["details"]], ["presets", "fps"])

    def test_create_refuses_non_object(self) -> None:
        with MockApi() as mock:
            status, envelope = send(mock, "POST", CREATE, as_json([CREATE_BODY]))

        self.assertEqual(status, HTTP_BAD_REQUEST)
        self.assertEqual(envelope["error"]["details"], [{"field": "body", "message": "Expected object, received array"}])

    def test_process_refuses_unknown_key(self) -> None:
        with MockApi("resume_uploading") as mock:
            status, envelope = send(mock, "POST", PROCESS, as_json({"preset": "ultra"}))
            launched = mock.state.scenes[SCENE_ID].launched

        self.assertEqual(status, HTTP_BAD_REQUEST)
        self.assertEqual(envelope["error"]["details"], [{"field": "preset", "message": "unrecognized key"}])
        self.assertFalse(launched)

    def test_process_takes_its_keys(self) -> None:
        with MockApi("resume_uploading") as mock:
            status, _ = send(mock, "POST", PROCESS, as_json({"enable_lod": False}))

        self.assertEqual(status, HTTP_OK)

    def test_process_empty_json_body_is_empty_object(self) -> None:
        with MockApi("resume_uploading") as mock:
            status, _ = send(mock, "POST", PROCESS, b"")

        self.assertEqual(status, HTTP_OK)


class SceneShape(unittest.TestCase):
    def test_no_preview_ready(self) -> None:
        with MockApi("resume") as mock:
            status, envelope = send(mock, "GET", SCENE)

        self.assertEqual(status, HTTP_OK)
        self.assertNotIn("preview_ready", envelope["data"])


if __name__ == "__main__":
    unittest.main()
