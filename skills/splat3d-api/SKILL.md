---
name: splat3d-api
description: Use when writing code that calls the Splat3D API (api.splat-3d.com) to turn a video, photo zip or COLMAP model into a 3D Gaussian splat scene, or when debugging such an integration.
---

# Splat3D API

Base URL `https://api.splat-3d.com`, every path under `/v1`. This file holds
the loop and the mistakes agents make; the docs hold the rest:
https://docs.splat-3d.com/llms-full.txt (all pages in one file) and
https://api.splat-3d.com/openapi.json (the spec). The same loop in Python
and curl: https://github.com/splat3d/examples.

## Auth

- Every call: `Authorization: Bearer s3d_...`, read from the `SPLAT3D_API_KEY`
  env var. Keys are created in the dashboard at
  https://splat-3d.com/dashboard/developer; there is no API for keys.
- Keep the key on a server. Never put it in browser code, a mobile app
  bundle or a repo. The API's CORS allows only splat-3d.com and
  localhost:3000/3001, so browser calls from your site fail anyway.

## The loop

1. `POST /v1/scenes` with JSON `{"content_type": "video/mp4", "file_size": <bytes>, "preset": "standard"}`.
   Answers `201` with `data.sceneId` and `data.uploadUrl` (camelCase; scene
   fields everywhere else are snake_case).
2. `PUT` the raw bytes to `uploadUrl` with `Content-Type` equal to the
   `content_type` from step 1 (the URL signs it). No `Authorization` header:
   the URL carries its own signature.
3. `POST /v1/scenes/{id}/process` with body `{}` and `Content-Type:
   application/json` (ARKit poses go in that body). This is the step that
   charges credits. Answers `200`; its `data.status` is always `processing`,
   a launch marker, not the scene's status.
4. Poll `GET /v1/scenes/{id}` every 5-10 s until `status` is `complete`,
   `failed` or `cancelled`.
5. `GET /v1/scenes/{id}/download?format=sog|ply|spz` answers `200` with the
   file bytes, to the owner's key only. The scene's `download_url` is this
   URL without `format`, so it sends the file that `format` names.

`preset` sets quality, run time and the charge
([presets](https://docs.splat-3d.com/guides/presets),
[pricing](https://docs.splat-3d.com/guides/pricing)):

| `preset` | Credits | Run time |
|---|---|---|
| `fast` | 10 | ~8 min |
| `standard` (default) | 20 | ~15 min |
| `quality` | 40 | ~25 min |
| `ultra` | 70 | ~45 min |
| `custom` | 20 | `standard`, plus your `sfm`, `training` and `output` overrides |

Run times leave out any wait for a GPU.

```ts
import { readFile, writeFile } from "node:fs/promises";

const API = "https://api.splat-3d.com/v1";
const auth = { Authorization: `Bearer ${process.env.SPLAT3D_API_KEY}` };
const asJson = { "Content-Type": "application/json" };
const TERMINAL = new Set(["complete", "failed", "cancelled"]);

// Unwrap data, or throw with the error code and request id. Not every answer
// is JSON: DELETE is a 204 with no body, and an edge error can be text or HTML.
async function call(path: string, init: RequestInit = {}) {
  const res = await fetch(`${API}${path}`, { ...init, headers: { ...auth, ...init.headers } });
  const isJson = res.headers.get("Content-Type")?.startsWith("application/json");
  const body = isJson ? await res.json() : { text: (await res.text()).slice(0, 200) };
  if (!res.ok) {
    const code = body.error?.code ?? body.text;
    throw new Error(`${res.status} ${code}: ${body.error?.message} (${body.meta?.request_id})`);
  }
  return body.data;
}

const file = await readFile("walkthrough.mp4");
const { sceneId, uploadUrl } = await call("/scenes", {
  method: "POST",
  headers: asJson,
  body: JSON.stringify({ content_type: "video/mp4", file_size: file.byteLength, preset: "standard" }),
});

const put = await fetch(uploadUrl, { method: "PUT", headers: { "Content-Type": "video/mp4" }, body: file });
if (!put.ok) {
  throw new Error(`upload failed: ${put.status}`);
}

await call(`/scenes/${sceneId}/process`, { method: "POST", headers: asJson, body: "{}" });

let scene;
do {
  await new Promise((r) => setTimeout(r, 10_000));
  scene = await call(`/scenes/${sceneId}`); // production code: retry a 503 after Retry-After
} while (!TERMINAL.has(scene.status));

if (scene.status !== "complete") {
  throw new Error(`scene ${scene.status}: ${scene.processing_error}`);
}
const model = await fetch(`${API}/scenes/${sceneId}/download?format=sog`, { headers: auth });
if (!model.ok) {
  throw new Error(`download failed: ${model.status}`);
}
await writeFile(`${sceneId}.${model.headers.get("X-Splat-Format")}`, Buffer.from(await model.arrayBuffer()));
console.log(scene.viewer_url);
```

## Traps

- **`/process` is one paid launch per scene.** Repeating it with the same
  body (and the same `Idempotency-Key`, if you send one) replays the saved
  result without charging again. A different body or key is `409 conflict`.
  After an uncertain answer (timeout, 503), repeat the same request or poll;
  never create a new scene to retry.
- **Create and `/retrain` are not retry-safe.** Each request makes a new
  scene, and create spends the monthly `scenes_created` quota. Check
  `GET /v1/scenes` before sending one again.
- **`429 queue_full`**: as many of your scenes are processing as your plan
  allows. Nothing was charged and the scene stays `uploading`. Wait
  `Retry-After` seconds (120), then repeat the same `/process`. `meta.limit`
  and `meta.current` give the numbers. `429 quota_exceeded` is the monthly
  quota (`meta.period`, `meta.limit`, `meta.field`): don't loop on it.
- **Error envelope**: `{"error": {"code", "message", "details"?}, "meta": {"request_id", ...}}`.
  Branch on `error.code`; treat a code you don't know by its HTTP status.
  Log `meta.request_id` (also in `X-Request-Id`). An error from the edge in
  front of the API can be text or HTML: check `Content-Type` before parsing,
  and send a named `User-Agent`. Codes worth handling:

  | Status | Code | Do |
  |---|---|---|
  | 400 | `invalid_input` | Fix the request; `details` lists `{field, message}` |
  | 400 | `source_missing` | The upload never landed: PUT again, then `/process` |
  | 402 | `insufficient_credits` | Nothing charged; the user needs credits |
  | 409 | `conflict` | Scene already launched, or a different body or key |
  | 413 | `payload_too_large` | File over 5 GB; nothing charged |
  | 415 | `invalid_input` | Send JSON bodies as `Content-Type: application/json` |
  | 422 | `source_unusable` | Empty file, not a zip, or under 5 photos: PUT a usable file to the same URL, `/process` again |
  | 422 | `invalid_input` | At create: a parameter outside the preset's tier; `details` has `allowed` and `hint` |
  | 429 | `queue_full`, `quota_exceeded` | See above |
  | 502 | `upstream_error` | Launch refused and refunded; repeating returns the same failure |
  | 503 | `service_unavailable`, `internal_error` | Retry after `Retry-After` only if the header is there |

- **`status` vs `processing_stage`.** `status` is the stored status, always
  set: `uploading`; `extracting_frames` from the launch; for videos the
  preview stages (`preview_extracting`, `preview_generating`,
  `preview_compressing`) and back to `extracting_frames`; then
  `estimating_poses`, `training`, `exporting`, `compressing`; then
  `complete`, `failed` or `cancelled`. A stage can repeat and more may be
  added: stop on the three terminal values only. `processing_stage` and
  `processing_pct` come live from the job, so they can name a stage
  `status` hasn't reached; show `processing_pct` as progress. Both are
  `null` unless processing. Failed scenes are refunded automatically;
  `processing_error` says why.
- **A queued launch looks stuck.** While it waits for a GPU it shows
  `extracting_frames` at 0%, for minutes when GPUs are busy. The API fails
  and refunds a stuck job itself (still queued after 90 minutes, or still
  processing after 150, checked every 10), so don't cancel or relaunch on a
  timer; a client timeout, if any, should exceed 160 minutes.
- **Formats.** `sog` (default) for web viewers, `ply` at full precision for
  tools and editing, `spz` for native viewers. `sog` and `ply` fall back to
  each other, so read `X-Splat-Format` for what came back. A scene without
  SPZ (older ones) answers `404` for `spz`, never another format.
- **Sharing.** There is no public or presigned file link: `/download` and
  `download_url` need the owner's key, so serve the file from your server.
  `viewer_url` (set on `complete`) opens only for the owner signed in at
  splat-3d.com until `PATCH /v1/scenes/{id}` with `{"is_public": true}`;
  then anyone with the link can open it, and `thumbnail_url` needs no key.
- **Cancel and delete.** `POST /v1/scenes/{id}/cancel` stops a job. It
  refunds the launch only within 10 s of the job starting, which is about
  when `/process` returns (`data.refunded`, `data.refunded_credits`).
  `DELETE /v1/scenes/{id}` removes the scene and its files (`204`, no
  body), cancelling an unfinished one first under the same rule.
- **Polling.** A `503` while polling is transient: keep going after its
  `Retry-After`. Optional: `webhook_url` (and `webhook_secret`) at create
  gets a POST on `complete` or `failed`, with up to 3 attempts; keep
  polling as the backstop.
- **Pagination.** `GET /v1/scenes?limit=1..100` (default 50). Pass
  `meta.next_cursor` back unchanged as `cursor`; `null` means the last page.
  It is opaque: never decode or build one.
- **Upload URL lifetime: 24 hours.** A second PUT before `/process` replaces
  the file. After 24 hours, create a new scene.
- **Inputs.** `content_type` `video/*` (the default when omitted, so a zip
  sent without it is read as a video), or `application/zip` for 5+ JPEG or
  PNG photos (HEIC and WebP are skipped) or a COLMAP workspace. At most 5 GB.
  Packaging, ARKit poses and limits:
  https://docs.splat-3d.com/guides/supported-inputs
