from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from comfyui_mcp_server.workflows import (
    WorkflowDefinitionError,
    WorkflowInputError,
    WorkflowRegistry,
    parse_workflow,
)

GRAPH = {
    "1": {"class_type": "CLIPTextEncode", "_meta": {"title": "Prompt"}, "inputs": {"text": "x", "clip": ["2", 1]}},
    "2": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "model.safetensors"}},
}


def test_bundled_workflows_load(workflows_dir: Path) -> None:
    registry = WorkflowRegistry([workflows_dir])
    assert set(registry.all()) == {"txt2img", "img2img"}
    assert registry.errors == {}
    schema = registry.get("txt2img").input_schema()
    assert schema["required"] == ["prompt"]
    # Defaults are read from the graph when the metadata does not set one.
    assert schema["properties"]["width"]["default"] == 512
    assert "default" not in schema["properties"]["seed"]


def test_targets_resolve_by_id_or_title() -> None:
    meta = {"parameters": {"a": {"target": "Prompt.text"}, "b": {"target": "2.inputs.ckpt_name"}}}
    workflow = parse_workflow("w", GRAPH, meta, Path("w.yaml"))
    assert workflow.parameters["a"].targets == [("1", "text")]
    assert workflow.parameters["b"].targets == [("2", "ckpt_name")]


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("Nope.text", "no node"),
        ("Prompt.missing", "has no input"),
        ("Prompt.clip", "wired to another node"),
        ("text", "must look like"),
    ],
)
def test_bad_targets_are_rejected(target: str, message: str) -> None:
    with pytest.raises(WorkflowDefinitionError, match=message):
        parse_workflow("w", GRAPH, {"parameters": {"a": {"target": target}}}, Path("w.yaml"))


def test_ui_format_is_rejected() -> None:
    with pytest.raises(WorkflowDefinitionError, match="Export \\(API\\)"):
        parse_workflow("w", {"nodes": [], "links": []}, {}, Path("w.json"))


def test_validate_and_build() -> None:
    meta = {
        "parameters": {
            "prompt": {"target": "Prompt.text", "required": True},
            "steps": {"type": "integer", "target": "Prompt.text", "minimum": 1},
            "seed": {"type": "integer", "target": "2.ckpt_name", "randomize": True},
        }
    }
    workflow = parse_workflow("w", GRAPH, meta, Path("w.yaml"))

    with pytest.raises(WorkflowInputError) as exc:
        workflow.validate({"steps": 0, "extra": 1})
    assert len(exc.value.problems) == 3  # unknown, minimum, required

    values = workflow.validate({"prompt": "a cat"})
    assert isinstance(values["seed"], int)
    graph = workflow.build(values, {"2.ckpt_name": "other.safetensors"})
    assert graph["1"]["inputs"]["text"] == "a cat"
    assert graph["2"]["inputs"]["ckpt_name"] == "other.safetensors"
    assert GRAPH["1"]["inputs"]["text"] == "x", "the template graph must not be mutated"


def test_integer_accepts_whole_floats_only() -> None:
    workflow = parse_workflow("w", GRAPH, {"parameters": {"n": {"type": "integer", "target": "Prompt.text"}}}, Path())
    assert workflow.validate({"n": 5.0}) == {"n": 5}
    with pytest.raises(WorkflowInputError):
        workflow.validate({"n": 5.5})
    with pytest.raises(WorkflowInputError):
        workflow.validate({"n": True})


def test_inline_workflow_and_broken_files_are_reported(tmp_path: Path) -> None:
    (tmp_path / "inline.yaml").write_text(json.dumps({"description": "inline", "workflow": GRAPH}))
    (tmp_path / "orphan.yaml").write_text("description: no graph\n")
    (tmp_path / "bad name.json").write_text(json.dumps(GRAPH))
    registry = WorkflowRegistry([tmp_path])
    assert list(registry.all()) == ["inline"]
    assert len(registry.errors) == 2


def test_registry_reloads_on_change(tmp_path: Path) -> None:
    registry = WorkflowRegistry([tmp_path])
    assert registry.all() == {}
    path = tmp_path / "new.json"
    path.write_text(json.dumps(GRAPH))
    assert list(registry.all()) == ["new"]
    path.unlink()
    assert registry.all() == {}


def test_later_directories_win(tmp_path: Path) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()
    (first / "w.yaml").write_text(json.dumps({"description": "first", "workflow": GRAPH}))
    (second / "w.yaml").write_text(json.dumps({"description": "second", "workflow": GRAPH}))
    assert WorkflowRegistry([first, second]).get("w").description == "second"


def test_configmap_bookkeeping_entries_are_ignored(tmp_path: Path) -> None:
    data = tmp_path / "..2026_09_15_00_00_00.000000"
    data.mkdir()
    (data / "w.json").write_text(json.dumps(GRAPH))
    os.symlink(data.name, tmp_path / "..data")
    os.symlink("..data/w.json", tmp_path / "w.json")
    registry = WorkflowRegistry([tmp_path])
    assert list(registry.all()) == ["w"]
    assert registry.errors == {}
