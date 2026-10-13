#!/usr/bin/env node
// Splat3D quickstart: a video or a zip of photos in, a 3D scene file out.
//
//   create -> upload -> process -> poll -> download
//
// Node 22+, built-ins only. Options, exit codes and errors are in ../README.md.
//
//   export SPLAT3D_API_KEY="s3d_..."
//   node quickstart.mjs capture.mp4

import { createWriteStream, existsSync, mkdirSync, openAsBlob, statSync } from "node:fs";
import { basename, extname, join } from "node:path";
import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";
import { setTimeout as sleep } from "node:timers/promises";
import { parseArgs } from "node:util";

const DEFAULT_API_BASE = "https://api.splat-3d.com";
const KEYS_PAGE = "https://splat-3d.com/dashboard/developer";
const BILLING_PAGE = "https://splat-3d.com/dashboard/billing";
const USER_AGENT = "splat3d-examples/0.1 (node)";
const RUNNER = "node quickstart.mjs";
const USAGE = `usage: ${RUNNER} FILE [--preset fast] [--format spz] [--out DIR]
       ${RUNNER} --scene-id ID [--format spz] [--out DIR]
Environment: SPLAT3D_API_KEY (required), SPLAT3D_API_BASE, SPLAT3D_POLL_SECONDS.`;

const DEFAULT_POLL_SECONDS = 10;
const MAX_WAIT_MINUTES = 150; // the API fails a scene still processing after this
const MAX_QUEUE_WAIT_MINUTES = 30;
const MAX_UNAVAILABLE_RETRIES = 5;
const ERROR_PREVIEW_BYTES = 500;
const TITLE_MAX_CHARS = 200;
const MS_PER_SECOND = 1000;
const SECONDS_PER_MINUTE = 60;

const EXIT_OK = 0;
const EXIT_FAILED = 1;
const EXIT_USAGE = 2;

const HTTP_NOT_FOUND = 404;
const HTTP_UNAVAILABLE = 503;

const CONTENT_TYPES = new Map([
  [".mp4", "video/mp4"],
  [".mov", "video/quicktime"],
  [".zip", "application/zip"],
]);
const PRESETS = ["fast", "standard", "quality", "ultra"];
const FORMATS = ["spz", "sog", "ply"];
const DEFAULT_OUT = ".";

const UPLOADING = "uploading";
const NO_REASON = "no reason given";
const TERMINAL_STATUSES = ["complete", "failed", "cancelled"];
// Every other status is a pipeline stage, e.g. extracting_frames or training.
const IDLE_STATUSES = [UPLOADING, ...TERMINAL_STATUSES];

// Extra advice for an error code, printed under the error.
const HINTS = new Map([
  ["source_unusable", "run again with a usable file"],
  ["payload_too_large", "the upload limit is 5 GB; send a shorter video or fewer photos"],
  ["unauthorized", `check SPLAT3D_API_KEY; keys are at ${KEYS_PAGE}`],
]);

const log = (line) => console.error(line);

// Ends the run with an exit code. Thrown, not process.exit(), so stderr
// written to a pipe is flushed first.
class Stop extends Error {
  constructor(exitCode) {
    super(`exit ${exitCode}`);
    this.exitCode = exitCode;
  }
}

// A non-2xx answer: the error envelope's fields, or the start of the raw body.
class ApiError extends Error {
  constructor(status, headers, bytes) {
    super(`HTTP ${status}`);
    this.status = status;
    this.retryAfter = retryAfter(headers.get("Retry-After"));
    this.raw = new TextDecoder().decode(bytes.subarray(0, ERROR_PREVIEW_BYTES));
    this.code = null;
    this.message = "";
    this.details = [];
    this.requestId = null;

    // { error: { code, message, details? }, meta: { request_id } }
    try {
      const envelope = JSON.parse(new TextDecoder().decode(bytes));
      if (typeof envelope?.error?.code === "string") {
        this.code = envelope.error.code;
        this.message = envelope.error.message;
        this.details = envelope.error.details ?? [];
        this.requestId = envelope.meta?.request_id ?? "unknown";
      }
    } catch {
      // Not the envelope, e.g. a proxy's page: report() prints the raw body.
    }
  }

  report() {
    if (this.code === null) {
      log(`error: HTTP ${this.status}: ${this.raw}`);
      return;
    }

    log(`error: HTTP ${this.status} ${this.code}: ${this.message} (request ${this.requestId})`);
    for (const detail of this.details) {
      log(`  ${describe(detail)}`);
    }
  }
}

// Retry-After in seconds. The API sends whole seconds, never a date.
function retryAfter(value) {
  if (value === null || !/^\s*\d+\s*$/.test(value)) {
    return null;
  }
  return Number(value);
}

// One `details` entry, in either of its two shapes.
//   A request that fails validation: `preset: Invalid enum value. ...`
//   A file that can't be used: `photo_count: 3 (allowed {"min":5}) Add JPEG or PNG photos.`
function describe(detail) {
  if (typeof detail.message === "string") {
    return `${detail.field}: ${detail.message}`;
  }

  const text = `${detail.field}: ${detail.value} (allowed ${JSON.stringify(detail.allowed ?? null)})`;
  return detail.hint ? `${text} ${detail.hint}` : text;
}

function fail(err, ...hints) {
  err.report();
  for (const hint of hints) {
    log(hint);
  }
  throw new Stop(EXIT_FAILED);
}

// fetch(); a non-2xx answer throws ApiError.
async function send(url, init) {
  const res = await fetch(url, init);
  if (!res.ok) {
    throw new ApiError(res.status, res.headers, new Uint8Array(await res.arrayBuffer()));
  }
  return res;
}

async function readData(res) {
  return (await res.json()).data;
}

// The Splat3D API: bearer key, a named User-Agent, JSON bodies.
class Api {
  constructor(base, key) {
    this.base = base.replace(/\/+$/, "");
    this.key = key;
  }

  open(method, path, body) {
    // A named agent: some hosts refuse a library's default one.
    const headers = { Authorization: `Bearer ${this.key}`, "User-Agent": USER_AGENT };
    const init = { method, headers };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }

    return send(this.base + path, init);
  }

  // open(), retrying a 503 that sends Retry-After: the API sends it only where a repeat is safe.
  async retrying(method, path, body) {
    for (let retries = 0; ; retries++) {
      try {
        return await this.open(method, path, body);
      } catch (err) {
        if (!(err instanceof ApiError) || err.status !== HTTP_UNAVAILABLE || err.retryAfter === null) {
          throw err;
        }
        if (retries === MAX_UNAVAILABLE_RETRIES) {
          throw err;
        }
        log(`unavailable (HTTP 503), retrying in ${err.retryAfter}s (${retries + 1}/${MAX_UNAVAILABLE_RETRIES})`);
        await sleep(err.retryAfter * MS_PER_SECOND);
      }
    }
  }
}

function scenePath(sceneId) {
  return `/v1/scenes/${encodeURIComponent(sceneId)}`;
}

function shellQuote(word) {
  return /^[\w./-]+$/.test(word) ? word : `'${word.replaceAll("'", `'\\''`)}'`;
}

// The command that picks this scene up again: poll, then download.
function resumeCommand(sceneId, format, out) {
  let command = `${RUNNER} --scene-id ${sceneId} --format ${format}`;
  if (out !== DEFAULT_OUT) {
    command += ` --out ${shellQuote(out)}`;
  }
  return `resume: ${command}`;
}

// POST /v1/scenes. Never retried: a repeat makes a second scene.
async function create(api, file, contentType, preset) {
  const body = {
    title: basename(file).slice(0, TITLE_MAX_CHARS),
    content_type: contentType,
    file_size: statSync(file).size,
    preset,
  };

  let data;
  try {
    data = await readData(await api.open("POST", "/v1/scenes", body));
  } catch (err) {
    if (!(err instanceof ApiError) || err.status !== HTTP_UNAVAILABLE) {
      throw err;
    }
    fail(err, "hint: create is not safe to repeat; check GET /v1/scenes for this scene before creating again");
  }

  log(`created scene ${data.scene_id}`);
  return data;
}

// PUT the file to storage, streamed from disk. The URL signs the Content-Type,
// so it must match create's; a file-backed Blob sends Content-Length. No API
// key: the URL is the credential.
async function upload(url, file, contentType) {
  const blob = await openAsBlob(file);
  try {
    const res = await send(url, {
      method: "PUT",
      body: blob,
      headers: { "Content-Type": contentType, "User-Agent": USER_AGENT },
    });
    await res.arrayBuffer();
  } catch (err) {
    if (!(err instanceof ApiError)) {
      throw err;
    }
    fail(err, "hint: the upload to storage failed; its URL lasts 24 hours");
  }

  log(`uploaded ${blob.size} bytes`);
}

// POST /process with `{}`, the same body every time, so a repeat replays the
// launch. queue_full charges nothing: wait Retry-After and send it again, for
// up to MAX_QUEUE_WAIT_MINUTES. insufficient_credits charges nothing either:
// the scene waits, uploaded, for --scene-id once credits are added.
async function start(api, sceneId, args) {
  const deadline = Date.now() + MAX_QUEUE_WAIT_MINUTES * SECONDS_PER_MINUTE * MS_PER_SECOND;
  for (;;) {
    try {
      await readData(await api.retrying("POST", `${scenePath(sceneId)}/process`, {}));
      log("processing started");
      return;
    } catch (err) {
      if (err instanceof ApiError && err.code === "insufficient_credits") {
        fail(err, `hint: add credits at ${BILLING_PAGE}`, resumeCommand(sceneId, args.format, args.out));
      }
      if (!(err instanceof ApiError) || err.code !== "queue_full" || err.retryAfter === null) {
        throw err;
      }
      if (Date.now() + err.retryAfter * MS_PER_SECOND > deadline) {
        fail(err, `hint: the wait for a free slot would pass ${MAX_QUEUE_WAIT_MINUTES} minutes; nothing was charged`,
          resumeCommand(sceneId, args.format, args.out));
      }
      log(`queue full: ${err.message} Waiting ${err.retryAfter}s.`);
      await sleep(err.retryAfter * MS_PER_SECOND);
    }
  }
}

// `processing: training 42%` while a stage runs, then e.g. `status: complete`.
// Only a running scene has a stage and a pct. Its status is the stored stage;
// processing_stage can be fresher.
function progress(scene) {
  if (IDLE_STATUSES.includes(scene.status)) {
    return `status: ${scene.status}`;
  }

  let line = `processing: ${scene.processing_stage || scene.status}`;
  if (scene.processing_pct !== null && scene.processing_pct !== undefined) {
    line += ` ${Math.floor(scene.processing_pct)}%`;
  }
  return line;
}

// Poll until the scene is complete, failed or cancelled. Any other status is still working.
async function wait(api, sceneId, args) {
  const deadline = Date.now() + MAX_WAIT_MINUTES * SECONDS_PER_MINUTE * MS_PER_SECOND;
  let shown = null;
  for (;;) {
    const scene = await readData(await api.retrying("GET", scenePath(sceneId)));

    // Print only what changed.
    const line = progress(scene);
    if (line !== shown) {
      log(line);
      shown = line;
    }

    if (TERMINAL_STATUSES.includes(scene.status)) {
      return scene;
    }

    if (Date.now() > deadline) {
      log(`error: scene ${sceneId} is still ${scene.status} after ${MAX_WAIT_MINUTES} minutes`);
      log(resumeCommand(sceneId, args.format, args.out));
      throw new Stop(EXIT_FAILED);
    }

    await sleep(args.pollSeconds * MS_PER_SECOND);
  }
}

// GET /download?format=..., streamed to <out>/scene-<id>.<format sent>.
async function download(api, sceneId, args) {
  let res;
  try {
    res = await api.retrying("GET", `${scenePath(sceneId)}/download?format=${args.format}`);
  } catch (err) {
    if (!(err instanceof ApiError) || err.status !== HTTP_NOT_FOUND || args.format !== "spz") {
      throw err;
    }
    fail(err, "hint: this scene has no SPZ file; try --format sog", resumeCommand(sceneId, "sog", args.out));
  }

  // sog and ply fall back to each other: name the file for what came.
  let sent = res.headers.get("X-Splat-Format");
  if (!FORMATS.includes(sent)) {
    sent = args.format;
  }

  mkdirSync(args.out, { recursive: true });
  const target = join(args.out, `scene-${sceneId}.${sent}`);
  await pipeline(Readable.fromWeb(res.body), createWriteStream(target));
  console.log(`saved ${target} (${statSync(target).size} bytes, ${sent})`);
}

async function run(api, args) {
  let sceneId;
  if (args.sceneId) {
    sceneId = args.sceneId;

    // Uploaded but never started (e.g. queue_full ran out): start it.
    const scene = await readData(await api.retrying("GET", scenePath(sceneId)));
    if (scene.status === UPLOADING) {
      await start(api, sceneId, args);
    }
  } else {
    const contentType = CONTENT_TYPES.get(extname(args.file).toLowerCase());
    const created = await create(api, args.file, contentType, args.preset);
    sceneId = created.scene_id;
    await upload(created.upload_url, args.file, contentType);
    await start(api, sceneId, args);
  }

  const scene = await wait(api, sceneId, args);
  if (scene.status === "failed") {
    log(`error: scene ${sceneId} failed: ${scene.processing_error ?? NO_REASON}`);
    log("hint: a failed scene is refunded automatically");
    return EXIT_FAILED;
  }
  if (scene.status === "cancelled") {
    log(`error: scene ${sceneId} was cancelled: ${scene.processing_error ?? NO_REASON}`);
    return EXIT_FAILED;
  }

  await download(api, sceneId, args);
  if (scene.viewer_url) {
    console.log(`viewer: ${scene.viewer_url}`);
  }
  return EXIT_OK;
}

function usage(message) {
  log(USAGE);
  log(`error: ${message}`);
  throw new Stop(EXIT_USAGE);
}

function parse() {
  let parsed;
  try {
    parsed = parseArgs({
      allowPositionals: true,
      options: {
        preset: { type: "string", default: "fast" },
        format: { type: "string", default: "spz" },
        out: { type: "string", default: DEFAULT_OUT },
        "scene-id": { type: "string" },
        help: { type: "boolean", short: "h" },
      },
    });
  } catch (err) {
    usage(err.message);
  }

  const { values, positionals } = parsed;
  if (values.help) {
    console.log(USAGE);
    throw new Stop(EXIT_OK);
  }

  // Usage errors exit 2 before any request.
  const file = positionals[0];
  const sceneId = values["scene-id"];
  if (positionals.length > 1) {
    usage("give one FILE");
  }
  if (Boolean(file) === Boolean(sceneId)) {
    usage("give a FILE or --scene-id, not both");
  }
  if (file && !(existsSync(file) && statSync(file).isFile())) {
    usage(`no such file: ${file}`);
  }
  if (file && !CONTENT_TYPES.has(extname(file).toLowerCase())) {
    usage(`unsupported file type ${extname(file) || "(none)"}: use .mp4, .mov or .zip`);
  }
  if (!PRESETS.includes(values.preset)) {
    usage(`--preset must be one of ${PRESETS.join(", ")}`);
  }
  if (!FORMATS.includes(values.format)) {
    usage(`--format must be one of ${FORMATS.join(", ")}`);
  }

  const pollSeconds = Number(process.env.SPLAT3D_POLL_SECONDS || DEFAULT_POLL_SECONDS);
  if (!Number.isFinite(pollSeconds)) {
    usage("SPLAT3D_POLL_SECONDS must be a number");
  }

  const key = process.env.SPLAT3D_API_KEY;
  if (!key) {
    usage(`set SPLAT3D_API_KEY; create a key at ${KEYS_PAGE}`);
  }

  return { file, sceneId, preset: values.preset, format: values.format, out: values.out, pollSeconds, key };
}

async function main() {
  try {
    const args = parse();
    const api = new Api(process.env.SPLAT3D_API_BASE || DEFAULT_API_BASE, args.key);
    return await run(api, args);
  } catch (err) {
    if (err instanceof Stop) {
      return err.exitCode;
    }
    if (err instanceof ApiError) {
      err.report();
      if (HINTS.has(err.code)) {
        log(`hint: ${HINTS.get(err.code)}`);
      }
      return EXIT_FAILED;
    }
    // fetch() reports a refused connection or DNS failure as a TypeError with a cause.
    if (err instanceof TypeError && err.cause) {
      log(`error: network: ${err.cause.message ?? err.cause}`);
      return EXIT_FAILED;
    }
    throw err;
  }
}

process.exitCode = await main();
