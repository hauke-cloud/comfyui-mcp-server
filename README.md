<!-- llm-readme-management spec=1 commit=4510bea81c8819a6fa61e3472e3e6f798489c9c6 template=python model=qwen3.8-27b-q4 digest=08ee779219f6 generated=2026-10-12T05:43:07Z -->
<a href="https://hauke.cloud" target="_blank"><img src="https://img.shields.io/badge/home-hauke.cloud-brightgreen" alt="hauke.cloud" style="display: block;" /></a>
<a href="https://github.com/hauke-cloud" target="_blank"><img src="https://img.shields.io/badge/github-hauke.cloud-blue" alt="hauke.cloud Github Organisation" style="display: block;" /></a>
<a href="https://github.com/hauke-cloud/llm-readme-management" target="_blank"><img src="https://img.shields.io/badge/template-python-orange" alt="Repository type - python" style="display: block;" /></a>


# Comfyui Mcp Server


<img src="https://raw.githubusercontent.com/hauke-cloud/.github/main/resources/img/organisation-logo-small.png" alt="hauke.cloud logo" width="109" height="123" align="right">


<llm header hint="Name the distribution and say whether it is a library, a CLI or a service.">

A Python service that exposes curated ComfyUI workflows as typed MCP tools and REST endpoints, executing them against a ComfyUI HTTP API over streamable HTTP. You deploy it via Docker or Helm in front of a ComfyUI instance so that MCP clients such as Claude and REST clients can generate images on a remote GPU.

</llm>


## :book: Description

<llm description>

comfyui-mcp-server is an MCP (Model Context Protocol) server and REST API that exposes ComfyUI workflows as typed, callable tools. You point it at a running ComfyUI instance (or a llama-swap-comfyui proxy), place workflow JSON files in a directory, and each becomes a tool with a JSON-schema parameter set. MCP clients such as Claude and plain HTTP clients can then submit image-generation jobs and receive output images without talking to ComfyUI directly.

The server patches the graph with caller-supplied parameters, submits it to ComfyUI's HTTP API, waits with progress notifications up to a configurable timeout, and returns download URLs or inline images. Workflow directories are hot-reloaded, so you can add or change workflows by updating a mounted volume or ConfigMap without a restart.

- One MCP tool per workflow file, with typed parameters (string, integer, number, boolean, image)
- REST endpoints mirroring every MCP tool for non-MCP clients
- Image inputs from ComfyUI's input directory, previous job outputs, base64/data URIs, or (opt-in) http(s) URLs
- Signed short-lived upload URLs for pushing files without base64 encoding
- Builtin tools: `list_workflows`, `run_workflow`, `get_job`, `cancel_job`, `queue_status`, `create_upload_url`, and (opt-in) `run_raw_workflow`

It ships as a multi-arch container image and a Helm chart under the `hauke

</llm>


## :clipboard: Requirements

<llm requirements hint="Give the Python version from requires-python or the CI matrix, and name the packaging tool actually in use (uv, poetry, pip).">

- Python ≥ 3.11 (CI and the container image build with 3.13); pip and a virtualenv for running from source.
- A reachable ComfyUI HTTP API (default `http://127.0.0.1:8188`) with the checkpoint files your workflows reference, or a llama-swap-comfyui proxy path.
- `API_TOKEN` set whenever the service is exposed to any network; `UPLOAD_SECRET` (or `API_TOKEN`) set explicitly for multi-replica deployments.
- Optionally `COMFYUI_API_KEY` if ComfyUI sits behind an authenticating proxy.
- For container deployment: Docker or a Kubernetes cluster, a `helm` client that can pull OCI charts, and a pull path to `ghcr.io`.
- An MCP client (e.g. Claude) with streamable-HTTP transport support and a tool-call timeout greater than `JOB_TIMEOUT` (default 110 s).

</llm>


## 🚀 Getting started

<llm getting_started hint="Use the project's real installation path -- editable install, uv sync, poetry install -- not a generic pip install.">

1. Clone the repository and enter the project directory.

```bash
git clone https://github.com/hauke-cloud/comfyui-mcp-server.git
cd comfyui-mcp-server
```

2. Create a virtual environment and activate it.

```bash
python -m venv .venv && . .venv/bin/activate
```

3. Install the runtime and development dependencies, then install the package in editable mode.

```bash
pip install -r requirements.txt -r requirements-dev.txt -e .
```

4. Start the server pointed at a local ComfyUI instance using the bundled example workflows.

```bash
COMFYUI_URL=http://127.0.0.1:8188 WORKFLOWS_DIR=./workflows comfyui-mcp-server
```

</llm>


## :airplane: Usage

<llm usage hint="Show the console-script entry points by name if the packaging metadata defines any.">

The console script `comfyui-mcp-server` starts the server. Point it at your ComfyUI instance and a directory of workflow files:

```bash
COMFYUI_URL=http://127.0.0.1:8188 WORKFLOWS_DIR=./workflows comfyui-mcp-server
```

The server listens on `0.0.0.0:8000` by default. Set `API_TOKEN` to enable bearer-token auth on `/mcp` and `/api/*`.

Once the server is running, you can invoke a workflow over REST. Each workflow in `WORKFLOWS_DIR` gets a `POST /api/workflows/<name>/run` endpoint. The bundled `txt2img` workflow accepts parameters such as `prompt` and `steps` (defined in its YAML metadata):

```bash
curl -s -X POST http://127.0.0.1:8000/api/workflows/txt2img/run \
  -H "Authorization: Bearer $API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "a lighthouse at dawn", "steps": 25}'
```

Append `?wait=true` to block until ComfyUI finishes (up to `JOB_TIMEOUT` seconds, default 110). Without it the response returns immediately with a `job_id` you can resume via the `get_job` tool.

For MCP clients such as Claude, register the streamable-HTTP endpoint:

```bash
claude mcp add --transport http comfyui \
  https://comfyui-mcp.example.com/mcp \
  --header "Authorization: Bearer $API_TOKEN"
```

Claude then sees one tool per workflow plus the built-in tools: `list_workflows`, `run_workflow`, `get_job`, `cancel_job`, `queue_status`, and `create_upload_url`.

</llm>


## :wrench: Configuration

<llm configuration>

The server reads configuration from environment variables defined in `src/comfyui_mcp_server/config.py`. The table covers the variables you are most likely to set; the remaining ones (`POLL_INTERVAL`, `COMFYUI_REQUEST_TIMEOUT`, `INLINE_MAX_BYTES`, `UPLOAD_URL_TTL`, `STATELESS_HTTP`, `ALLOWED_HOSTS`, `HOST`, `PORT`, `LOG_LEVEL`) are in that file.

| Name | Default | Description |
|------|---------|-------------|
| `COMFYUI_URL` | `http://127.0.0.1:8188` | Base URL of the ComfyUI HTTP API; path prefixes (e.g. llama-swap) are supported. |
| `API_TOKEN` | *(empty)* | Bearer token for `/mcp` and `/api/*`; empty disables auth. Required whenever the service is exposed. |
| `WORKFLOWS_DIR` | `/app/workflows` | `:`-separated directories scanned for workflow files; later entries win on name clash. |
| `JOB_TIMEOUT` | `110` | Seconds a tool call waits before returning the job id as `pending`. |
| `PUBLIC_URL` | *(empty)* | External base URL for output links; empty yields relative links. |
| `INLINE_IMAGES` | `true` | Return image outputs inline in MCP results. |
| `ALLOW_URL_INPUTS` | `false` | Permit image parameters fetched from http(s) URLs (SSRF risk). |
| `ENABLE_RAW_WORKFLOWS` | `false` | Expose the `run_raw_workflow` tool for arbitrary graphs. |
| `UPLOAD_SECRET` | *(empty)* | HMAC key for signed upload URLs; set explicitly for multi-replica deployments. |
| `MAX_UPLOAD_BYTES` | `52428800` | Largest accepted upload (50 MiB). |

For Helm, the chart at `deployment/helm/comfyui-mcp-server/values.yaml` mirrors these under `config.*`, adds `auth.*` (rendered into a Secret), `workflows.files` (rendered into a ConfigMap), and optional `ingress.*`, `httpRoute.*`, `envoyGateway.*`, and `autoscaling.*` blocks. See that file for the complete value set.

</llm>


## :hammer: Development

<llm development hint="Cover the test runner, the linter and any pre-commit hooks the repository configures.">

Install the package and its development dependencies in a virtualenv:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt -e .
```

Run the test suite (no GPU or ComfyUI instance needed; tests use an in-process fake):

```bash
pytest
```

Lint and format-check with Ruff:

```bash
ruff check .
ruff format --check .
```

CI runs all three of the above on every pull request to `main` or `develop`, plus a multi-arch Docker build. It also lints and renders the Helm chart:

```bash
helm lint ./deployment/helm/comfyui-mcp-server
helm template ci ./deployment/helm/comfyui-mcp-server > /dev/null
```

A guard step verifies that `deployment/helm/comfyui-mcp-server/values.yaml` contains exactly one `tag:` line. CI rewrites that line to the release version at publish time, so introducing a second `tag:` key will fail the build.

Pull-request titles are validated by a separate workflow: the type must be one of `fix`, `feat`, `docs`, `ci`, or `chore`, and the subject must begin with an uppercase letter.

No pre-commit hooks are configured in this repository.

</llm>


## 📄 License

This Project is licensed under the GNU General Public License v3.0

- see the [LICENSE](LICENSE) file for details.


## :coffee: Contributing

To become a contributor, please check out the [CONTRIBUTING](CONTRIBUTING.md) file.


## :email: Contact

For any inquiries or support requests, please open an issue in this
repository or contact us at [contact@hauke.cloud](mailto:contact@hauke.cloud).
