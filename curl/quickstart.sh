#!/usr/bin/env bash
# Splat3D quickstart: a video or a zip of photos in, a 3D scene file out.
#
#   create -> upload -> process -> poll -> download
#
# bash 3.2+ (macOS's default), curl and jq. Options, exit codes and errors
# are in ../README.md.
#
#   export SPLAT3D_API_KEY="s3d_..."
#   ./quickstart.sh capture.mp4

set -euo pipefail

readonly DEFAULT_API_BASE="https://api.splat-3d.com"
readonly KEYS_PAGE="https://splat-3d.com/dashboard/developer"
readonly BILLING_PAGE="https://splat-3d.com/dashboard/billing"
readonly USER_AGENT="splat3d-examples/0.1 (curl)"
readonly RUNNER="./quickstart.sh"
readonly USAGE="usage: $RUNNER FILE [--preset fast] [--format spz] [--out DIR]
       $RUNNER --scene-id ID [--format spz] [--out DIR]
Environment: SPLAT3D_API_KEY (required), SPLAT3D_API_BASE, SPLAT3D_POLL_SECONDS."

readonly DEFAULT_POLL_SECONDS=10
readonly MAX_WAIT_MINUTES=150 # the API fails a scene still processing after this
readonly MAX_QUEUE_WAIT_MINUTES=30
readonly MAX_UNAVAILABLE_RETRIES=5
readonly ERROR_PREVIEW_BYTES=500
readonly TITLE_MAX_CHARS=200
readonly SECONDS_PER_MINUTE=60

readonly EXIT_OK=0
readonly EXIT_FAILED=1
readonly EXIT_USAGE=2

readonly HTTP_NOT_FOUND=404
readonly HTTP_UNAVAILABLE=503

readonly PRESETS="fast standard quality ultra"
readonly FORMATS="spz sog ply"
readonly DEFAULT_OUT="."
readonly UPLOADING="uploading"
readonly NO_REASON="no reason given"

log() {
  printf '%s\n' "$*" >&2
}

usage_error() {
  log "$USAGE"
  log "error: $1"
  exit "$EXIT_USAGE"
}

network_error() {
  log "error: network: curl exit $1"
  exit "$EXIT_FAILED"
}

# Whether a word is in a space-separated list.
one_of() {
  case " $2 " in
    *" $1 "*) return 0 ;;
  esac
  return 1
}

# The content type for a file name, by extension; empty when unsupported.
content_type_of() {
  case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
    *.mp4) echo "video/mp4" ;;
    *.mov) echo "video/quicktime" ;;
    *.zip) echo "application/zip" ;;
    *) echo "" ;;
  esac
}

# -- requests ---------------------------------------------------------------
#
# Every request leaves its status in STATUS, its body in $BODY and its
# headers in $HEADERS.

# api METHOD PATH [JSON]: one API call. The key goes to curl in a file, so it
# never shows in the process list; a named agent, since some hosts refuse
# curl's default one.
api() {
  local method=$1 path=$2 json=${3-}
  set -- -sS -X "$method" -D "$HEADERS" -o "$BODY" -w '%{http_code}' -H "@$AUTH" -A "$USER_AGENT"
  if [ -n "$json" ]; then
    set -- "$@" -H "Content-Type: application/json" --data-binary "$json"
  fi
  STATUS=$(curl "$@" "$API_BASE$path") || network_error $?
}

# api(), retrying a 503 that sends Retry-After: the API sends it only where a
# repeat is safe.
api_retrying() {
  local retries=0 wait
  while :; do
    api "$@"
    if [ "$STATUS" != "$HTTP_UNAVAILABLE" ]; then
      return 0
    fi

    wait=$(retry_after)
    if [ -z "$wait" ] || [ "$retries" -eq "$MAX_UNAVAILABLE_RETRIES" ]; then
      return 0
    fi
    retries=$((retries + 1))
    log "unavailable (HTTP 503), retrying in ${wait}s ($retries/$MAX_UNAVAILABLE_RETRIES)"
    sleep "$wait"
  done
}

ok() {
  case "$STATUS" in
    2[0-9][0-9]) return 0 ;;
  esac
  return 1
}

# The last value of a response header. The file can hold a 100 Continue first.
header() {
  grep -i "^$1:" "$HEADERS" | tail -n 1 | sed 's/^[^:]*:[[:space:]]*//' | tr -d '\r' || true
}

# Retry-After in seconds; empty when absent. The API sends whole seconds.
retry_after() {
  local value
  value=$(header Retry-After)
  case "$value" in
    '' | *[!0-9]*) echo "" ;;
    *) echo "$value" ;;
  esac
}

# The error envelope's code, or empty when the body isn't the envelope:
# { error: { code, message, details? }, meta: { request_id } }
error_code() {
  jq -r 'if (.error.code | type) == "string" then .error.code else empty end' "$BODY" 2>/dev/null || true
}

# Print an API error: code, message, request id and each `details` entry, in
# either of its two shapes:
#   a request that fails validation: `preset: Invalid enum value. ...`
#   a file that can't be used: `photo_count: 3 (allowed {"min":5}) Add JPEG or PNG photos.`
# A body that isn't the envelope (e.g. a proxy's page) prints raw.
report_error() {
  if [ -z "$(error_code)" ]; then
    log "error: HTTP $STATUS: $(head -c "$ERROR_PREVIEW_BYTES" "$BODY")"
    return
  fi

  log "$(jq -r --arg status "$STATUS" \
    '"error: HTTP \($status) \(.error.code): \(.error.message) (request \(.meta.request_id // "unknown"))"' "$BODY")"
  jq -r '.error.details // [] | .[]
    | if (.message | type) == "string" then "  \(.field): \(.message)"
      else "  \(.field): \(.value) (allowed \(.allowed | tojson))" + (if .hint then " \(.hint)" else "" end)
      end' \
    "$BODY" >&2
}

# Report the error, with advice for its code, and stop.
refuse() {
  report_error
  case "$(error_code)" in
    source_unusable) log "hint: run again with a usable file" ;;
    payload_too_large) log "hint: the upload limit is 5 GB; send a shorter video or fewer photos" ;;
    unauthorized) log "hint: check SPLAT3D_API_KEY; keys are at $KEYS_PAGE" ;;
  esac
  exit "$EXIT_FAILED"
}

# The command that picks this scene up again: poll, then download.
resume_command() {
  local command="$RUNNER --scene-id $SCENE_ID --format $1"
  if [ "$OUT" != "$DEFAULT_OUT" ]; then
    command="$command --out $(printf '%q' "$OUT")"
  fi
  printf 'resume: %s\n' "$command"
}

# -- steps ------------------------------------------------------------------

# POST /v1/scenes. Never retried: a repeat makes a second scene.
create() {
  local size title json
  size=$(wc -c <"$FILE" | tr -d ' ')
  title=$(basename "$FILE")
  json=$(jq -cn --arg title "${title:0:$TITLE_MAX_CHARS}" --arg type "$CONTENT_TYPE" \
    --argjson size "$size" --arg preset "$PRESET" \
    '{title: $title, content_type: $type, file_size: $size, preset: $preset}')

  api POST /v1/scenes "$json"
  if [ "$STATUS" = "$HTTP_UNAVAILABLE" ]; then
    report_error
    log "hint: create is not safe to repeat; check GET /v1/scenes for this scene before creating again"
    exit "$EXIT_FAILED"
  fi
  if ! ok; then
    refuse
  fi

  SCENE_ID=$(jq -r '.data.sceneId' "$BODY")
  UPLOAD_URL=$(jq -r '.data.uploadUrl' "$BODY")
  log "created scene $SCENE_ID"
}

# PUT the file to storage. -T streams it from disk with a Content-Length. The
# URL signs the Content-Type, so it must match create's. No API key: the URL
# is the credential.
upload() {
  STATUS=$(curl -sS -T "$FILE" -H "Content-Type: $CONTENT_TYPE" -A "$USER_AGENT" \
    -D "$HEADERS" -o "$BODY" -w '%{http_code}' "$UPLOAD_URL") || network_error $?
  if ! ok; then
    report_error
    log "hint: the upload to storage failed; its URL lasts 24 hours"
    exit "$EXIT_FAILED"
  fi

  log "uploaded $(wc -c <"$FILE" | tr -d ' ') bytes"
}

# POST /process with `{}`, the same body every time, so a repeat replays the
# launch. queue_full charges nothing: wait Retry-After and send it again, for
# up to MAX_QUEUE_WAIT_MINUTES. insufficient_credits charges nothing either:
# the scene waits, uploaded, for --scene-id once credits are added.
start() {
  local deadline wait
  deadline=$(($(date +%s) + MAX_QUEUE_WAIT_MINUTES * SECONDS_PER_MINUTE))
  while :; do
    api_retrying POST "$SCENE_PATH/process" '{}'
    if ok; then
      log "processing started"
      return 0
    fi

    if [ "$(error_code)" = "insufficient_credits" ]; then
      report_error
      log "hint: add credits at $BILLING_PAGE"
      log "$(resume_command "$FORMAT")"
      exit "$EXIT_FAILED"
    fi

    wait=$(retry_after)
    if [ "$(error_code)" != "queue_full" ] || [ -z "$wait" ]; then
      refuse
    fi
    if [ $(($(date +%s) + wait)) -gt "$deadline" ]; then
      report_error
      log "hint: the wait for a free slot would pass $MAX_QUEUE_WAIT_MINUTES minutes; nothing was charged"
      log "$(resume_command "$FORMAT")"
      exit "$EXIT_FAILED"
    fi

    log "queue full: $(jq -r '.error.message' "$BODY") Waiting ${wait}s."
    sleep "$wait"
  done
}

# One poll's progress line: `processing: training 42%` while a stage runs,
# then e.g. `status: complete`. Only a running scene has a stage and a pct.
# Its status is the stored stage; processing_stage can be fresher.
progress() {
  case "$1" in
    "$UPLOADING" | complete | failed | cancelled) echo "status: $1" ;;
    *)
      jq -r '.data | "processing: \(.processing_stage // .status)"
        + (if .processing_pct != null then " \(.processing_pct | floor)%" else "" end)' "$BODY"
      ;;
  esac
}

# Poll until the scene is complete, failed or cancelled; any other status is
# still working. Prints each change and keeps the last scene in $SCENE.
wait_for_scene() {
  local deadline line shown="" status
  deadline=$(($(date +%s) + MAX_WAIT_MINUTES * SECONDS_PER_MINUTE))
  while :; do
    api_retrying GET "$SCENE_PATH"
    if ! ok; then
      refuse
    fi

    status=$(jq -r '.data.status' "$BODY")
    line=$(progress "$status")
    if [ "$line" != "$shown" ]; then
      log "$line"
      shown=$line
    fi

    case "$status" in
      complete | failed | cancelled)
        cp "$BODY" "$SCENE"
        return 0
        ;;
    esac

    if [ "$(date +%s)" -gt "$deadline" ]; then
      log "error: scene $SCENE_ID is still $status after $MAX_WAIT_MINUTES minutes"
      log "$(resume_command "$FORMAT")"
      exit "$EXIT_FAILED"
    fi

    sleep "$POLL_SECONDS"
  done
}

# GET /download?format=..., saved as <out>/scene-<id>.<format sent>.
download() {
  local sent target
  api_retrying GET "$SCENE_PATH/download?format=$FORMAT"
  if [ "$STATUS" = "$HTTP_NOT_FOUND" ] && [ "$FORMAT" = "spz" ]; then
    report_error
    log "hint: this scene has no SPZ file; try --format sog"
    log "$(resume_command sog)"
    exit "$EXIT_FAILED"
  fi
  if ! ok; then
    refuse
  fi

  # sog and ply fall back to each other: name the file for what came.
  sent=$(header X-Splat-Format)
  if ! one_of "$sent" "$FORMATS"; then
    sent=$FORMAT
  fi

  mkdir -p "$OUT"
  target="$OUT/scene-$SCENE_ID.$sent"
  mv "$BODY" "$target"
  printf 'saved %s (%s bytes, %s)\n' "$target" "$(wc -c <"$target" | tr -d ' ')" "$sent"
}

# -- arguments --------------------------------------------------------------

need_value() {
  if [ "$#" -lt 2 ]; then
    usage_error "$1 needs a value"
  fi
}

parse_args() {
  FILE="" SCENE_ID="" PRESET="fast" FORMAT="spz" OUT=$DEFAULT_OUT
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --preset | --format | --out | --scene-id) need_value "$@" ;;
    esac

    case "$1" in
      --preset) PRESET=$2; shift ;;
      --preset=*) PRESET=${1#*=} ;;
      --format) FORMAT=$2; shift ;;
      --format=*) FORMAT=${1#*=} ;;
      --out) OUT=$2; shift ;;
      --out=*) OUT=${1#*=} ;;
      --scene-id) SCENE_ID=$2; shift ;;
      --scene-id=*) SCENE_ID=${1#*=} ;;
      -h | --help)
        printf '%s\n' "$USAGE"
        exit "$EXIT_OK"
        ;;
      -*) usage_error "unknown option $1" ;;
      *)
        if [ -n "$FILE" ]; then
          usage_error "give one FILE"
        fi
        FILE=$1
        ;;
    esac
    shift
  done
}

# Usage errors exit 2 before any request.
validate() {
  if [ -n "$FILE" ] && [ -n "$SCENE_ID" ] || [ -z "$FILE$SCENE_ID" ]; then
    usage_error "give a FILE or --scene-id, not both"
  fi
  if [ -n "$FILE" ] && [ ! -f "$FILE" ]; then
    usage_error "no such file: $FILE"
  fi

  CONTENT_TYPE=""
  if [ -n "$FILE" ]; then
    CONTENT_TYPE=$(content_type_of "$FILE")
  fi
  if [ -n "$FILE" ] && [ -z "$CONTENT_TYPE" ]; then
    case "$(basename "$FILE")" in
      *.*) usage_error "unsupported file type .${FILE##*.}: use .mp4, .mov or .zip" ;;
      *) usage_error "unsupported file type (none): use .mp4, .mov or .zip" ;;
    esac
  fi

  if ! one_of "$PRESET" "$PRESETS"; then
    usage_error "--preset must be one of $PRESETS"
  fi
  if ! one_of "$FORMAT" "$FORMATS"; then
    usage_error "--format must be one of $FORMATS"
  fi

  POLL_SECONDS=${SPLAT3D_POLL_SECONDS:-$DEFAULT_POLL_SECONDS}
  case "$POLL_SECONDS" in
    '' | *[!0-9.]* | *.*.*) usage_error "SPLAT3D_POLL_SECONDS must be a number" ;;
  esac

  if [ -z "${SPLAT3D_API_KEY:-}" ]; then
    usage_error "set SPLAT3D_API_KEY; create a key at $KEYS_PAGE"
  fi
  API_BASE=${SPLAT3D_API_BASE:-$DEFAULT_API_BASE}
  API_BASE=${API_BASE%/}
}

main() {
  parse_args "$@"
  validate

  WORK=$(mktemp -d "${TMPDIR:-/tmp}/splat3d.XXXXXX")
  trap 'rm -rf "$WORK"' EXIT
  BODY="$WORK/body" HEADERS="$WORK/headers" SCENE="$WORK/scene" AUTH="$WORK/auth"
  (
    umask 077
    printf 'Authorization: Bearer %s\n' "$SPLAT3D_API_KEY" >"$AUTH"
  )

  if [ -n "$SCENE_ID" ]; then
    SCENE_PATH="/v1/scenes/$(jq -rn --arg id "$SCENE_ID" '$id | @uri')"

    # Uploaded but never started (e.g. queue_full ran out): start it.
    api_retrying GET "$SCENE_PATH"
    if ! ok; then
      refuse
    fi
    if [ "$(jq -r '.data.status' "$BODY")" = "$UPLOADING" ]; then
      start
    fi
  else
    create
    SCENE_PATH="/v1/scenes/$(jq -rn --arg id "$SCENE_ID" '$id | @uri')"
    upload
    start
  fi

  wait_for_scene
  case "$(jq -r '.data.status' "$SCENE")" in
    failed)
      log "error: scene $SCENE_ID failed: $(jq -r --arg none "$NO_REASON" '.data.processing_error // $none' "$SCENE")"
      log "hint: a failed scene is refunded automatically"
      exit "$EXIT_FAILED"
      ;;
    cancelled)
      log "error: scene $SCENE_ID was cancelled: $(jq -r --arg none "$NO_REASON" '.data.processing_error // $none' "$SCENE")"
      exit "$EXIT_FAILED"
      ;;
  esac

  download
  local viewer
  viewer=$(jq -r '.data.viewer_url // empty' "$SCENE")
  if [ -n "$viewer" ]; then
    printf 'viewer: %s\n' "$viewer"
  fi
}

main "$@"
