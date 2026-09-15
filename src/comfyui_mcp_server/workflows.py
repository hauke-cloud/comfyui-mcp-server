"""Workflow definitions: loading, parameter schemas and graph patching.

A workflow is a ComfyUI graph in API format ("Export (API)" in the UI) plus
optional metadata that turns selected node inputs into named parameters:

    workflows/txt2img.json   the graph
    workflows/txt2img.yaml   description, parameters, outputs

The metadata may also carry the graph inline under ``workflow:``, which is
convenient for Helm values. Directories are rescanned whenever a file changes,
so a remounted ConfigMap is picked up without a restart.
"""

from __future__ import annotations

import copy
import json
import logging
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
PARAM_TYPES = {"string", "integer", "number", "boolean", "image"}
_MISSING = object()
# Largest seed that survives a round trip through JSON numbers in every client.
MAX_RANDOM_SEED = 2**53 - 1


class WorkflowDefinitionError(ValueError):
    """A workflow file is malformed."""


class WorkflowInputError(ValueError):
    """Parameters supplied for a run do not match the workflow's schema."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass
class Parameter:
    name: str
    type: str
    targets: list[tuple[str, str]]
    description: str = ""
    required: bool = False
    default: Any = _MISSING
    enum: list[Any] | None = None
    minimum: float | None = None
    maximum: float | None = None
    randomize: bool = False

    def json_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {}
        if self.type == "image":
            schema["type"] = "string"
            accepted = "a base64 string or data: URI, an http(s) URL if the server allows it, or"
            hint = f"{accepted} 'output:<subfolder>/<filename>' to reuse a previous job's output"
            schema["description"] = f"{self.description} ({hint})".strip()
        else:
            schema["type"] = self.type
            if self.description:
                schema["description"] = self.description
        if self.enum is not None:
            schema["enum"] = self.enum
        if self.minimum is not None:
            schema["minimum"] = self.minimum
        if self.maximum is not None:
            schema["maximum"] = self.maximum
        if self.default is not _MISSING:
            schema["default"] = self.default
        if self.randomize:
            schema["description"] = f"{schema.get('description', '')} Random when omitted.".strip()
        return schema

    def check(self, value: Any) -> str | None:
        """Return a problem description, or None if the value is acceptable."""
        ok = {
            "string": isinstance(value, str),
            "image": isinstance(value, str),
            "boolean": isinstance(value, bool),
            "integer": isinstance(value, int)
            and not isinstance(value, bool)
            or isinstance(value, float)
            and value.is_integer(),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        }[self.type]
        if not ok:
            return f"{self.name}: expected {self.type}, got {type(value).__name__}"
        if self.enum is not None and value not in self.enum:
            return f"{self.name}: must be one of {self.enum}"
        if self.minimum is not None and value < self.minimum:
            return f"{self.name}: must be >= {self.minimum}"
        if self.maximum is not None and value > self.maximum:
            return f"{self.name}: must be <= {self.maximum}"
        return None


@dataclass
class Workflow:
    name: str
    graph: dict[str, Any]
    source: Path
    title: str = ""
    description: str = ""
    parameters: dict[str, Parameter] = field(default_factory=dict)
    # Node ids whose outputs are returned; empty means every output node.
    outputs: list[str] = field(default_factory=list)

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {name: p.json_schema() for name, p in self.parameters.items()},
            "required": [name for name, p in self.parameters.items() if p.required],
            "additionalProperties": False,
        }

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title or self.name,
            "description": self.description,
            "parameters": self.input_schema(),
        }

    def validate(self, values: dict[str, Any] | None) -> dict[str, Any]:
        """Check caller values and fill in randomised ones. Omitted optional
        parameters are left out so the graph keeps its own values."""
        values = dict(values or {})
        problems = [f"unknown parameter {name!r}" for name in values if name not in self.parameters]
        resolved: dict[str, Any] = {}
        for name, param in self.parameters.items():
            if name in values and values[name] is not None:
                problem = param.check(values[name])
                if problem:
                    problems.append(problem)
                elif param.type == "integer":
                    resolved[name] = int(values[name])
                else:
                    resolved[name] = values[name]
            elif param.randomize:
                resolved[name] = random.randint(0, MAX_RANDOM_SEED)
            elif param.required:
                problems.append(f"{name}: required")
        if problems:
            raise WorkflowInputError(problems)
        return resolved

    def build(self, values: dict[str, Any], overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return a copy of the graph with validated values applied.

        ``overrides`` maps raw ``<node id or title>.<input>`` addresses to
        values, for workflows without declared parameters.
        """
        graph = copy.deepcopy(self.graph)
        for name, value in values.items():
            for node_id, input_name in self.parameters[name].targets:
                graph[node_id]["inputs"][input_name] = value
        problems = []
        for address, value in (overrides or {}).items():
            try:
                node_id, input_name = resolve_target(graph, address)
            except WorkflowDefinitionError as exc:
                problems.append(str(exc))
                continue
            graph[node_id]["inputs"][input_name] = value
        if problems:
            raise WorkflowInputError(problems)
        return graph


def resolve_target(graph: dict[str, Any], address: str) -> tuple[str, str]:
    """Resolve ``<node id or title>.<input>`` (``inputs.`` optional) to ids."""
    node_ref, sep, input_name = str(address).rpartition(".")
    if not sep or not node_ref or not input_name:
        raise WorkflowDefinitionError(f"target {address!r} must look like '<node id or title>.<input>'")
    node_ref = node_ref.removesuffix(".inputs")
    node_id = resolve_node(graph, node_ref)
    inputs = graph[node_id].get("inputs", {})
    if input_name not in inputs:
        raise WorkflowDefinitionError(f"target {address!r}: node {node_id} has no input {input_name!r}")
    if _is_link(inputs[input_name]):
        raise WorkflowDefinitionError(f"target {address!r}: input is wired to another node, not a value")
    return node_id, input_name


def resolve_node(graph: dict[str, Any], ref: str) -> str:
    if ref in graph:
        return ref
    matches = [node_id for node_id, node in graph.items() if node.get("_meta", {}).get("title") == ref]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise WorkflowDefinitionError(f"node title {ref!r} is ambiguous (nodes {', '.join(matches)})")
    raise WorkflowDefinitionError(f"no node with id or title {ref!r}")


def _is_link(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and isinstance(value[1], int)


def check_graph(graph: Any) -> dict[str, Any]:
    if isinstance(graph, dict) and "nodes" in graph and "links" in graph:
        raise WorkflowDefinitionError("this is a UI-format workflow; export it with 'Export (API)' instead")
    if not isinstance(graph, dict) or not graph:
        raise WorkflowDefinitionError("workflow graph must be a non-empty object of nodes")
    for node_id, node in graph.items():
        if not isinstance(node, dict) or "class_type" not in node or not isinstance(node.get("inputs"), dict):
            raise WorkflowDefinitionError(f"node {node_id!r} is not an API-format node (class_type + inputs)")
    return graph


def parse_workflow(name: str, graph: Any, meta: dict[str, Any], source: Path) -> Workflow:
    if not NAME_RE.match(name):
        raise WorkflowDefinitionError(f"workflow name {name!r} must match {NAME_RE.pattern}")
    graph = check_graph(graph)

    parameters: dict[str, Parameter] = {}
    for param_name, spec in (meta.get("parameters") or {}).items():
        if not NAME_RE.match(param_name):
            raise WorkflowDefinitionError(f"parameter name {param_name!r} must match {NAME_RE.pattern}")
        if not isinstance(spec, dict):
            raise WorkflowDefinitionError(f"parameter {param_name!r} must be a mapping")
        param_type = spec.get("type", "string")
        if param_type not in PARAM_TYPES:
            raise WorkflowDefinitionError(f"parameter {param_name!r}: type must be one of {sorted(PARAM_TYPES)}")
        raw_targets = spec.get("target", spec.get("targets"))
        if not raw_targets:
            raise WorkflowDefinitionError(f"parameter {param_name!r} needs a target")
        if isinstance(raw_targets, str):
            raw_targets = [raw_targets]
        try:
            targets = [resolve_target(graph, t) for t in raw_targets]
        except WorkflowDefinitionError as exc:
            raise WorkflowDefinitionError(f"parameter {param_name!r}: {exc}") from exc

        param = Parameter(
            name=param_name,
            type=param_type,
            targets=targets,
            description=str(spec.get("description", "")),
            required=bool(spec.get("required", False)),
            enum=spec.get("enum"),
            minimum=spec.get("minimum"),
            maximum=spec.get("maximum"),
            randomize=bool(spec.get("randomize", False)),
        )
        if "default" in spec:
            param.default = spec["default"]
        elif not param.required and not param.randomize and param.type != "image":
            # Advertise what the graph would use anyway.
            node_id, input_name = targets[0]
            param.default = graph[node_id]["inputs"][input_name]
        parameters[param_name] = param

    outputs = []
    for ref in meta.get("outputs") or []:
        try:
            outputs.append(resolve_node(graph, str(ref)))
        except WorkflowDefinitionError as exc:
            raise WorkflowDefinitionError(f"outputs: {exc}") from exc

    return Workflow(
        name=name,
        graph=graph,
        source=source,
        title=str(meta.get("title", "")),
        description=str(meta.get("description", "")).strip(),
        parameters=parameters,
        outputs=outputs,
    )


def _load_dir(directory: Path) -> tuple[dict[str, Workflow], dict[str, str]]:
    workflows: dict[str, Workflow] = {}
    errors: dict[str, str] = {}
    by_stem: dict[str, dict[str, Path]] = {}
    for path in sorted(directory.iterdir()):
        # Skips Kubernetes' ..data / ..<timestamp> ConfigMap bookkeeping entries.
        if path.name.startswith(".") or not path.is_file():
            continue
        kind = {".json": "graph", ".yaml": "meta", ".yml": "meta"}.get(path.suffix.lower())
        if kind:
            by_stem.setdefault(path.stem, {})[kind] = path

    for stem, files in by_stem.items():
        source = files.get("meta") or files["graph"]
        try:
            meta: dict[str, Any] = {}
            if "meta" in files:
                meta = yaml.safe_load(files["meta"].read_text()) or {}
                if not isinstance(meta, dict):
                    raise WorkflowDefinitionError("metadata must be a mapping")
            if "workflow" in meta:
                graph = meta["workflow"]
                if isinstance(graph, str):
                    graph = json.loads(graph)
            elif "graph" in files:
                graph = json.loads(files["graph"].read_text())
            else:
                raise WorkflowDefinitionError(f"no {stem}.json next to it and no inline 'workflow' key")
            workflow = parse_workflow(str(meta.get("name", stem)), graph, meta, source)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            errors[str(source)] = str(exc)
            logger.warning("skipping workflow %s: %s", source, exc)
            continue
        workflows[workflow.name] = workflow
    return workflows, errors


class WorkflowRegistry:
    def __init__(self, directories: list[Path]):
        self.directories = directories
        self._signature: tuple[Any, ...] | None = None
        self._workflows: dict[str, Workflow] = {}
        self.errors: dict[str, str] = {}

    def _current_signature(self) -> tuple[Any, ...]:
        entries = []
        for directory in self.directories:
            try:
                for path in sorted(directory.iterdir()):
                    stat = path.stat()
                    entries.append((str(path), stat.st_mtime_ns, stat.st_size))
            except OSError:
                entries.append((str(directory), None, None))
        return tuple(entries)

    def refresh(self) -> None:
        signature = self._current_signature()
        if signature == self._signature:
            return
        workflows: dict[str, Workflow] = {}
        errors: dict[str, str] = {}
        for directory in self.directories:
            if not directory.is_dir():
                logger.info("workflow directory %s does not exist", directory)
                continue
            found, failed = _load_dir(directory)
            workflows.update(found)
            errors.update(failed)
        self._workflows, self.errors, self._signature = workflows, errors, signature
        logger.info("loaded %d workflow(s): %s", len(workflows), ", ".join(sorted(workflows)) or "-")

    def all(self) -> dict[str, Workflow]:
        self.refresh()
        return self._workflows

    def get(self, name: str) -> Workflow | None:
        return self.all().get(name)
