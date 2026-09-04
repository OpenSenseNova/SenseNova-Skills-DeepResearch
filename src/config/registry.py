from __future__ import annotations

import base64
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

import yaml

from deepresearch_cli.prompting import PromptBundle

from .models import NodeSpec, SpecValidationError
from .paths import builtin_asset_dir, builtin_config_dir


class NodeRegistryError(ValueError):
    pass


_BINARY_RESOURCE_PREFIX = "deepresearch-resource-base64-v1:"


def builtin_node_dir() -> Path:
    return builtin_config_dir() / "nodes"


class NodeRegistry:
    """Load immutable node capabilities from flat YAML registries."""

    def __init__(self, nodes: Mapping[str, NodeSpec]) -> None:
        self._nodes = dict(nodes)

    @classmethod
    def load(
        cls,
        extra_dirs: Sequence[Path] = (),
    ) -> "NodeRegistry":
        roots = [builtin_node_dir(), *(Path(item) for item in extra_dirs)]
        bundle = PromptBundle.load()
        nodes: Dict[str, NodeSpec] = {}
        for root in roots:
            if not root.exists():
                continue
            if root.is_symlink() or not root.is_dir():
                raise NodeRegistryError(f"node registry root is unsafe: {root}")
            for spec_path in sorted(root.glob("*.yaml")):
                if spec_path.is_symlink() or not spec_path.is_file():
                    raise NodeRegistryError(f"node config is unsafe: {spec_path}")
                spec = _load_node(spec_path, bundle)
                if spec.node_id in nodes:
                    raise NodeRegistryError(f"duplicate node id: {spec.node_id}")
                nodes[spec.node_id] = spec
        if not nodes:
            raise NodeRegistryError("no node configurations were found")
        return cls(nodes)

    @classmethod
    def from_snapshot(cls, values: Sequence[Mapping[str, Any]]) -> "NodeRegistry":
        nodes = [NodeSpec.from_dict(value) for value in values]
        if len({item.node_id for item in nodes}) != len(nodes):
            raise NodeRegistryError("node snapshot contains duplicate ids")
        return cls({item.node_id: item for item in nodes})

    def get(self, node_id: str) -> NodeSpec:
        try:
            return self._nodes[node_id]
        except KeyError:
            raise KeyError(node_id)

    def list(self) -> tuple[NodeSpec, ...]:
        return tuple(self._nodes[key] for key in sorted(self._nodes))

    def snapshot_for(self, node_ids: Iterable[str]) -> list[Mapping[str, Any]]:
        selected = []
        seen = set()
        for node_id in node_ids:
            if node_id in seen:
                continue
            seen.add(node_id)
            selected.append(self.get(node_id).to_dict())
        return selected


def _load_node(
    spec_path: Path,
    bundle: PromptBundle,
) -> NodeSpec:
    node_dir = spec_path.parent
    node_name = spec_path.stem
    try:
        raw = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise NodeRegistryError(f"cannot load node {node_name}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise NodeRegistryError(f"node {node_name} YAML must contain an object")
    value = dict(raw)
    allowed = {
        "version", "id", "kind", "inputs", "outputs", "prompt",
        "prompt_bundle_key", "prompt_bundle_resources", "command",
        "preparer", "prepared_outputs", "materializer", "validator", "validators", "resources",
        "repair_on_validation_failure",
    }
    unknown = set(value) - allowed
    if unknown:
        raise NodeRegistryError(
            f"node {node_name} has unknown keys: {', '.join(sorted(unknown))}"
        )
    if str(value.get("version")) != "1":
        raise NodeRegistryError(f"node {node_name} requires version: 1")
    if value.get("id") != node_name:
        raise NodeRegistryError(
            f"node config {spec_path.name} must match node id {value.get('id')!r}"
        )

    prompt_file = value.pop("prompt", None)
    prompt_key = value.pop("prompt_bundle_key", None)
    command = value.get("command", ())
    preparer = value.get("preparer", ())
    materializer = value.get("materializer", ())
    validator = value.get("validator", ())
    validators = value.get("validators", ())
    resources = _load_declared_resources(node_dir, value.pop("resources", {}))
    bundle_resources = value.pop("prompt_bundle_resources", {})
    if bundle_resources:
        if not isinstance(bundle_resources, Mapping):
            raise NodeRegistryError(
                f"node {node_name} prompt_bundle_resources must be an object"
            )
        for name, bundle_key in bundle_resources.items():
            try:
                resources[str(name)] = bundle.resources[str(bundle_key)]
            except KeyError as exc:
                raise NodeRegistryError(
                    f"node {node_name} references unknown bundle resource {bundle_key}"
                ) from exc

    prompt: Optional[str] = None
    if prompt_file is not None and prompt_key is not None:
        raise NodeRegistryError(f"node {node_name} cannot use prompt and prompt_bundle_key")
    if prompt_file is not None:
        prompt = _read_child(node_dir, prompt_file, "prompt")
    elif prompt_key is not None:
        try:
            prompt = bundle.node_prompts[str(prompt_key)]
        except KeyError as exc:
            raise NodeRegistryError(
                f"node {node_name} references unknown prompt bundle key {prompt_key}"
            ) from exc

    value["prompt"] = prompt
    value["resources"] = resources
    normalized_command, command_resources = _normalize_command(command, node_dir)
    normalized_preparer, preparer_resources = _normalize_command(preparer, node_dir)
    normalized_materializer, materializer_resources = _normalize_command(
        materializer, node_dir
    )
    normalized_validator, validator_resources = _normalize_command(validator, node_dir)
    normalized_validators = []
    validators_resources: Dict[str, str] = {}
    if validators not in (None, [], ()):
        if not isinstance(validators, list):
            raise NodeRegistryError(f"node {node_name} validators must be a list")
        for item in validators:
            normalized, item_resources = _normalize_command(item, node_dir)
            normalized_validators.append(normalized)
            validators_resources.update(item_resources)
    resources.update(command_resources)
    resources.update(preparer_resources)
    resources.update(materializer_resources)
    resources.update(validator_resources)
    resources.update(validators_resources)
    value["command"] = normalized_command
    value["preparer"] = normalized_preparer
    value["materializer"] = normalized_materializer
    value["validator"] = normalized_validator
    value["validators"] = normalized_validators
    try:
        return NodeSpec.from_dict(value)
    except (SpecValidationError, AttributeError, TypeError) as exc:
        raise NodeRegistryError(f"invalid node {node_name}: {exc}") from exc


def _normalize_command(value: Any, node_dir: Path) -> tuple[list[str], Dict[str, str]]:
    if value in (None, [], ()):
        return [], {}
    if not isinstance(value, list) or not value or any(not isinstance(x, str) or not x for x in value):
        raise NodeRegistryError(f"node {node_dir.name} command must be a string array")
    result = []
    resources: Dict[str, str] = {}
    for item in value:
        if item.startswith("./"):
            relative = item[2:]
            content = _read_child(node_dir, relative, "command resource")
            key = "command/" + relative
            resources[key] = content
            result.append(f"resource:{key}")
        else:
            result.append(item)
    return result, resources


def _load_declared_resources(node_dir: Path, value: Any) -> Dict[str, str]:
    if value in (None, {}):
        return {}
    if not isinstance(value, Mapping):
        raise NodeRegistryError(f"node {node_dir.name} resources must be an object")
    result = {}
    for name, relative in value.items():
        if not isinstance(name, str) or not name:
            raise NodeRegistryError("resource names must be non-empty text")
        resource = PurePosixPath(name)
        if resource.is_absolute() or any(
            part in {"", ".", ".."} for part in resource.parts
        ):
            raise NodeRegistryError(f"unsafe resource name: {name}")
        if isinstance(relative, str) and relative.startswith("asset:"):
            asset_path = relative.split(":", 1)[1]
            result[name] = _read_resource(
                builtin_asset_dir(), asset_path, "built-in asset"
            )
        else:
            result[name] = _read_resource(node_dir, relative, "resource")
    return result


def _read_resource(root: Path, value: Any, label: str) -> str:
    path = _resolve_child(root, value, label)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise NodeRegistryError(f"cannot read {label} {path}: {exc}") from exc
    if not data:
        raise NodeRegistryError(f"{label} is empty: {path}")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return _BINARY_RESOURCE_PREFIX + base64.b64encode(data).decode("ascii")


def _read_child(root: Path, value: Any, label: str) -> str:
    path = _resolve_child(root, value, label)
    return path.read_text(encoding="utf-8")


def _resolve_child(root: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise NodeRegistryError(f"{label} path must be non-empty text")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise NodeRegistryError(f"unsafe {label} path: {value}")
    path = root.joinpath(*relative.parts)
    if path.is_symlink() or not path.is_file():
        raise NodeRegistryError(f"{label} file not found: {path}")
    resolved_root = root.resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise NodeRegistryError(f"{label} escapes node config directory: {value}") from exc
    return resolved
