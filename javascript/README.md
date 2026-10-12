# Splat3D quickstart: JavaScript (Node)

Needs Node 22+. Built-ins only: nothing to install.

```bash
export SPLAT3D_API_KEY="s3d_..."
node quickstart.mjs capture.mp4                  # --preset fast --format spz --out .
node quickstart.mjs --scene-id a1b2c3d4e5f6      # resume a scene
npm start -- capture.mp4                         # the same, through npm
```

This runs in Node only. The API refuses browser requests from other sites (CORS), and a browser
must never hold an API key: call the API from your server.

See [../README.md](../README.md) for options, exit codes and errors.
