# Splat3D quickstart: curl

Needs bash 3.2+ (macOS's default), curl and jq.

```bash
export SPLAT3D_API_KEY="s3d_..."
./quickstart.sh capture.mp4                  # --preset fast --format spz --out .
./quickstart.sh --scene-id a1b2c3d4e5f6      # resume a scene
```

The script adds what the steps below leave out: `queue_full` waits, 503 retries and error
reporting. Its create also sends the file's size (`file_size`), so the API refuses a file over
the 5 GB upload limit before anything is uploaded. See [../README.md](../README.md) for options, exit codes and errors.

## The five steps by hand

```bash
export SPLAT3D_API_KEY="s3d_..."
API=https://api.splat-3d.com
FILE=capture.mp4

# 1. Create a scene. The upload URL lasts 24 hours.
RESPONSE=$(curl -sS -X POST "$API/v1/scenes" \
  -H "Authorization: Bearer $SPLAT3D_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"title": "My scene", "content_type": "video/mp4", "preset": "fast"}')
SCENE_ID=$(echo "$RESPONSE" | jq -r .data.scene_id)
UPLOAD_URL=$(echo "$RESPONSE" | jq -r .data.upload_url)

# 2. Upload. Same Content-Type as step 1, and no API key.
curl -sS -T "$FILE" -H "Content-Type: video/mp4" "$UPLOAD_URL"

# 3. Start processing. This charges the credits (fast: 10).
curl -sS -X POST "$API/v1/scenes/$SCENE_ID/process" \
  -H "Authorization: Bearer $SPLAT3D_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{}'

# 4. Poll every 10 seconds until status is complete, failed or cancelled.
#    Until then status is the pipeline stage, e.g. training, with its progress.
curl -sS "$API/v1/scenes/$SCENE_ID" \
  -H "Authorization: Bearer $SPLAT3D_API_KEY" \
  | jq '.data | {status, processing_stage, processing_pct}'

# 5. Download.
curl -fsS -o "scene-$SCENE_ID.spz" "$API/v1/scenes/$SCENE_ID/download?format=spz" \
  -H "Authorization: Bearer $SPLAT3D_API_KEY"
```

For a zip of photos, use `"content_type": "application/zip"` in step 1 and
`-H "Content-Type: application/zip"` in step 2.
