# comfyui-mcp-server

An MCP server that turns curated ComfyUI workflows into tools. Every workflow
you drop into a directory becomes a typed MCP tool (`txt2img(prompt, seed, …)`)
and a REST endpoint, executed against ComfyUI's HTTP API.

```
MCP client ──/mcp──┐
                   ├─ comfyui-mcp-server ──HTTP──> ComfyUI  (or llama-swap /upstream/comfyui)
REST client ─/api──┘         │
                     workflows/*.json + *.yaml
```

- **Streamable HTTP**, stateless by default, so replicas need no sticky sessions
- **Workflows as tools** with JSON-schema parameters, defaults, ranges and enums
- **Results inline**: generated images come back as MCP image content plus download URLs
- **Image inputs** via signed upload URLs, base64/data URI, a previous job's output, or (opt-in) a URL
- **Hot reload**: edited workflow files and remounted ConfigMaps are picked up live
- **Bearer auth**, probes that never touch ComfyUI, OCI image and Helm chart

### Why not the official `comfy-mcp`?

[Comfy-Org/comfy-mcp](https://github.com/Comfy-Org/comfy-mcp) is a stdio
server that runs `comfy-cli` commands as subprocesses to control the ComfyUI
installed on the *same machine*: launching it, installing nodes and
downloading models. Behind a stdio→HTTP bridge in a cluster, most of those
tools would act on the container instead of your ComfyUI. Its `run_workflow`
also takes a file path rather than exposing workflows as tools. This server
does the opposite: it doesn't manage ComfyUI at all, and serves a fixed,
reviewed set of workflows to many clients.

## Workflows

A workflow is a graph exported from ComfyUI with **Export (API)**, plus
optional metadata that names the inputs a caller may set:

```
workflows/
  txt2img.json   # the API-format graph
  txt2img.yaml   # description, parameters, outputs
```

```yaml
title: Text to image
description: Generate an image from a text prompt.
parameters:
  prompt:
    type: string            # string | integer | number | boolean | image
    required: true
    description: What the image should show
    target: Positive Prompt.text    # "<node id or node title>.<input>"
  width:
    type: integer
    minimum: 64
    maximum: 2048
    target: Empty Latent Image.width   # default is read from the graph
  seed:
    type: integer
    randomize: true         # a fresh random value when omitted
    target: [KSampler.seed] # one parameter may set several inputs
outputs: [Save Image]       # only return these nodes' files (default: all)
```

- Targets resolve by node id (`"6.text"`) or by node title (`"Positive Prompt.text"`).
  Titles survive re-exports, so set meaningful titles in the ComfyUI editor.
- Everything is checked at load time. A typo in a target, a UI-format export or
  a target wired to another node disables that one workflow and is reported
  under `load_errors` in `/api/workflows` and `/readyz`.
- The graph may be inlined in the YAML under `workflow:` instead of a separate
  `.json`, which is handy in Helm values.
- Omitted optional parameters keep the value stored in the graph.
- A `.json` without a `.yaml` is still served. It has no parameters, but
  `run_workflow` can set any input through `overrides`
  (`{"6.text": "a cat"}`).

`image` parameters upload the file into ComfyUI's input directory and accept:

| Value | Meaning |
|---|---|
| `input:<filename>` | a file already in ComfyUI's input directory, e.g. from an upload (below) |
| `iVBORw0…` / `data:image/png;base64,…` | inline image |
| `output:<subfolder>/<filename>` | reuse a previous job's output (chain workflows) |
| `https://…` | fetched by the server, only with `ALLOW_URL_INPUTS=true` |

### Uploading local images

Base64 puts every byte of the image into the model's tool-call arguments,
which is slow, expensive and easily truncated for photos. Clients with a shell
or HTTP client should upload instead:

1. `create_upload_url` returns a short-lived URL (`UPLOAD_URL_TTL`) and the
   command to use.
2. `curl -sS -T photo.jpg 'https://…/uploads/<token>'` sends the raw file and
   answers `{"image": "input:mcp_upload_….jpg"}`.
3. That value goes into the workflow's image parameter.

The URL's HMAC token authenticates the upload, so `/uploads/` needs no bearer
token and the shell never sees it. Tokens are signed with `UPLOAD_SECRET`,
falling back to `API_TOKEN`; with several replicas set one of them. REST
clients can also `POST /api/uploads` with the bearer token. Uploads must be
PNG, JPEG, WebP, GIF, BMP or TIFF and at most `MAX_UPLOAD_BYTES`.

The image ships two examples, [`txt2img`](workflows/txt2img.yaml) and
[`img2img`](workflows/img2img.yaml), built on the stock SD 1.5 graph.

## MCP tools

| Tool | |
|---|---|
| `<workflow name>` | one per workflow; waits for the result and returns images + URLs |
| `list_workflows` | names, descriptions and parameter schemas |
| `run_workflow` | run by name, with `parameters`, `overrides` and `wait` |
| `get_job` | status and outputs of a job, waiting by default |
| `cancel_job` | drop a queued job or interrupt a running one |
| `queue_status` | what ComfyUI is running and has queued |
| `create_upload_url` | a signed URL for uploading a local image file |
| `run_raw_workflow` | arbitrary API-format graph, only with `ENABLE_RAW_WORKFLOWS=true` |

A tool call waits up to `JOB_TIMEOUT` and sends progress notifications while
the job is queued or running. If the job is still going after that, the call
returns its `job_id` with status `pending` or `running`, and `get_job` picks it
up again.

A ComfyUI cold start (e.g. llama-swap loading it on the first call) holds the
submission open. The call keeps sending progress notifications meanwhile, and
that time counts against the same `JOB_TIMEOUT`, so a slow start still returns
a `job_id` before the client gives up instead of leaving the job orphaned.

```bash
claude mcp add --transport http comfyui https://comfyui-mcp.example.com/mcp \
  --header "Authorization: Bearer $API_TOKEN"
```

## REST API

| Method | Path | |
|---|---|---|
| `GET` | `/api/workflows` | list workflows (+ `load_errors`) |
| `GET` | `/api/workflows/{name}` | one workflow's schema; `?graph=true` adds the graph |
| `POST` | `/api/workflows/{name}/run` | body: parameters, or `{"parameters": …, "overrides": …}`; `?wait=false` returns 202 at once |
| `GET` | `/api/jobs/{id}` | job status and outputs; `?wait=true` blocks until done |
| `DELETE` | `/api/jobs/{id}` | cancel |
| `GET` | `/api/queue` | ComfyUI queue |
| `GET` | `/api/view?filename=&subfolder=&type=` | download an output file |
| `POST`/`PUT` | `/api/uploads` | raw image body; returns `{"image": "input:…"}` |
| `POST`/`PUT` | `/uploads/{token}` | same, authenticated by a `create_upload_url` token |
| `GET` | `/healthz`, `/readyz` | probes (unauthenticated, never call ComfyUI) |

```bash
curl -s -X POST https://comfyui-mcp.example.com/api/workflows/txt2img/run \
  -H "Authorization: Bearer $API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"prompt": "a lighthouse at dawn", "steps": 25}'
```

`200` completed or failed (see `status`), `202` still running, `422` invalid
parameters, `4xx`/`502` rejected by or unable to reach ComfyUI.

## Configuration

| Variable | Default | |
|---|---|---|
| `COMFYUI_URL` | `http://127.0.0.1:8188` | ComfyUI base URL; a path prefix is fine |
| `COMFYUI_API_KEY` | | sent as `Authorization: Bearer` to ComfyUI or its proxy |
| `COMFYUI_REQUEST_TIMEOUT` | `600` | seconds per ComfyUI request (cold starts behind llama-swap) |
| `WORKFLOWS_DIR` | `/app/workflows` | `:`-separated directories; later ones win on name clash |
| `JOB_TIMEOUT` | `110` | seconds a call waits, cold start included, before returning the job id; keep it below your MCP clients' tool timeout |
| `POLL_INTERVAL` | `1` | seconds between status polls |
| `API_TOKEN` | | require `Authorization: Bearer <token>` on `/mcp` and `/api` |
| `PUBLIC_URL` | | external base URL used for output links |
| `INLINE_IMAGES` | `true` | return images inline in MCP results |
| `INLINE_MAX_BYTES` | `5242880` | total inline image budget per result |
| `ALLOW_URL_INPUTS` | `false` | let image parameters be fetched from URLs |
| `UPLOAD_SECRET` | `API_TOKEN` | key signing upload URLs; per-process random when both are empty |
| `UPLOAD_URL_TTL` | `600` | seconds an upload URL stays valid |
| `MAX_UPLOAD_BYTES` | `52428800` | largest accepted upload |
| `ENABLE_RAW_WORKFLOWS` | `false` | expose `run_raw_workflow` |
| `STATELESS_HTTP` | `true` | stateless streamable HTTP |
| `ALLOWED_HOSTS` | | comma-separated `Host` allow-list (DNS rebinding protection) |
| `HOST` / `PORT` / `LOG_LEVEL` | `0.0.0.0` / `8000` / `info` | |

ComfyUI itself has no authentication, and anything that can reach this server
can run workflows on your GPU. Set `API_TOKEN` whenever the service is exposed.

### With llama-swap-comfyui

Point `COMFYUI_URL` at llama-swap's proxy path, e.g.
`http://llama-swap:8080/upstream/comfyui`. The first tool call loads ComfyUI,
which evicts the resident LLM. Probes never call ComfyUI, so they don't
trigger swaps. If llama-swap swaps ComfyUI out while a job runs, the job is
reported as `failed` ("job disappeared") and doesn't hang until the timeout.

## Deployment

### Container

```bash
docker run --rm -p 8000:8000 \
  -e COMFYUI_URL=http://host.docker.internal:8188 \
  -e API_TOKEN=change-me \
  -v ./my-workflows:/workflows:ro -e WORKFLOWS_DIR=/app/workflows:/workflows \
  ghcr.io/hauke-cloud/comfyui-mcp-server:latest
```

### Helm

```bash
helm install comfyui-mcp oci://ghcr.io/hauke-cloud/charts/comfyui-mcp-server \
  --set config.comfyuiUrl=http://llama-swap.ai.svc:8080/upstream/comfyui \
  --set auth.apiToken=change-me \
  --set-file 'workflows.files.sdxl\.json=./sdxl.json' \
  --set-file 'workflows.files.sdxl\.yaml=./sdxl.yaml'
```

Workflows come from the image (`workflows.includeBundled`), a ConfigMap
rendered from `workflows.files` or named by `workflows.existingConfigMap`, and
any `workflows.extraDirs` you mount. See
[`values.yaml`](deployment/helm/comfyui-mcp-server/values.yaml).

The chart can expose the service through an `ingress` or, for clusters that
run a Gateway API implementation, through `httpRoute`. Either route's request
timeout must outlast `config.jobTimeout`, which the HTTPRoute defaults already
do. With Envoy Gateway, `envoyGateway.securityPolicy` and
`envoyGateway.backendTrafficPolicy` attach policies to that route. For
example, validate Keycloak JWTs and rate-limit at the gateway:

```yaml
httpRoute:
  enabled: true
  parentRefs:
    - name: hauke-cloud
      namespace: envoy-gateway
  hostnames: [comfyui-mcp.example.com]
envoyGateway:
  securityPolicy:
    enabled: true
    spec:
      jwt:
        providers:
          - name: keycloak
            issuer: https://id.hauke.cloud/realms/cloud
            remoteJWKS:
              uri: https://id.hauke.cloud/realms/cloud/protocol/openid-connect/certs
  backendTrafficPolicy:
    enabled: true
    spec:
      rateLimit:
        type: Local
        local:
          rules:
            - limit: {requests: 30, unit: Minute}
```

### CI/CD

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) lints and renders the
chart, then runs
[`inpacken-un-af-dor-mit`](https://github.com/hauke-cloud/inpacken-un-af-dor-mit).
That action runs ruff and pytest, builds a multi-arch image to
`ghcr.io/hauke-cloud/comfyui-mcp-server` and pushes the chart to
`oci://ghcr.io/hauke-cloud/charts`.

- **push to `main`**: image `latest`, `main`, `0.0.0-dev-<sha>` and chart `0.0.0-dev-<sha>`
- **tag `v1.2.3`**: image and chart `1.2.3`, plus a GitHub release
- **pull request**: lint, test and build without pushing

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt -e .
pytest && ruff check . && ruff format --check .

COMFYUI_URL=http://127.0.0.1:8188 WORKFLOWS_DIR=./workflows comfyui-mcp-server
```

The tests run against an in-process fake of ComfyUI's HTTP API, so they need
neither a GPU nor ComfyUI.

## Licence

MIT
