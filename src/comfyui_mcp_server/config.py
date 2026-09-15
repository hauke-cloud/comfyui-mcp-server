"""Runtime configuration, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _list(name: str, default: list[str], sep: str = ",") -> list[str]:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return [item.strip() for item in raw.split(sep) if item.strip()]


@dataclass(frozen=True)
class Settings:
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "info"

    # ComfyUI HTTP API. May carry a path prefix, e.g. llama-swap's
    # http://llama-swap:8080/upstream/comfyui
    comfyui_url: str = "http://127.0.0.1:8188"
    comfyui_api_key: str = ""
    # Generous on purpose: behind llama-swap the first request cold-starts
    # ComfyUI, and the proxy holds that request open until it is ready.
    comfyui_request_timeout: float = 600.0

    # Directories scanned for workflow definitions, later ones win on name clash.
    workflows_dirs: list[Path] = field(default_factory=lambda: [Path("/app/workflows")])

    # How long a tool call waits for a job before handing back its id instead.
    # Kept under the ~120s many MCP clients allow a tool call: a client that
    # gives up first never learns the job id it could have polled.
    job_timeout: float = 110.0
    poll_interval: float = 1.0

    # Bearer token required on /mcp and /api/*; empty disables auth.
    api_token: str = ""
    # Absolute base URL clients reach this server at, used for output links.
    public_url: str = ""

    inline_images: bool = True
    inline_max_bytes: int = 5 * 1024 * 1024

    # Allow parameters of type image to be fetched from http(s) URLs. Off by
    # default because it lets callers make the server issue arbitrary requests.
    allow_url_inputs: bool = False
    # Expose run_raw_workflow, which executes arbitrary API-format graphs.
    enable_raw_workflows: bool = False

    # Stateless streamable HTTP needs no sticky sessions, so replicas scale freely.
    stateless_http: bool = True
    # Host header allow-list for DNS rebinding protection; empty disables it.
    allowed_hosts: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> Settings:
        dirs = _list("WORKFLOWS_DIR", ["/app/workflows"], sep=os.pathsep)
        return cls(
            host=os.environ.get("HOST", cls.host),
            port=_int("PORT", cls.port),
            log_level=os.environ.get("LOG_LEVEL", cls.log_level).lower(),
            comfyui_url=os.environ.get("COMFYUI_URL", cls.comfyui_url).rstrip("/"),
            comfyui_api_key=os.environ.get("COMFYUI_API_KEY", ""),
            comfyui_request_timeout=_float("COMFYUI_REQUEST_TIMEOUT", cls.comfyui_request_timeout),
            workflows_dirs=[Path(d) for d in dirs],
            job_timeout=_float("JOB_TIMEOUT", cls.job_timeout),
            poll_interval=_float("POLL_INTERVAL", cls.poll_interval),
            api_token=os.environ.get("API_TOKEN", ""),
            public_url=os.environ.get("PUBLIC_URL", "").rstrip("/"),
            inline_images=_bool("INLINE_IMAGES", cls.inline_images),
            inline_max_bytes=_int("INLINE_MAX_BYTES", cls.inline_max_bytes),
            allow_url_inputs=_bool("ALLOW_URL_INPUTS", cls.allow_url_inputs),
            enable_raw_workflows=_bool("ENABLE_RAW_WORKFLOWS", cls.enable_raw_workflows),
            stateless_http=_bool("STATELESS_HTTP", cls.stateless_http),
            allowed_hosts=_list("ALLOWED_HOSTS", []),
        )
