from __future__ import annotations

import hashlib
import json
import os
import sysconfig
from dataclasses import dataclass, field, replace
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Dict, Mapping, Optional


class PromptBundleError(ValueError):
    pass


SUPPORTED_BUNDLE_VERSION = "0.9"


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _default_bundle_dir() -> Path:
    configured = os.environ.get("DEEPRESEARCH_PROMPT_DIR")
    if configured:
        return Path(configured).expanduser().resolve()

    source_tree = Path(__file__).resolve().parents[2] / "prompts"
    if source_tree.is_dir():
        return source_tree

    installed = Path(sysconfig.get_path("data")) / "share" / "deepresearch-cli" / "prompts"
    return installed


def _safe_child(root: Path, name: str) -> Path:
    if not name or Path(name).name != name:
        raise PromptBundleError(f"invalid prompt filename: {name!r}")
    path = (root / name).resolve()
    if path.parent != root.resolve():
        raise PromptBundleError(f"prompt escapes bundle directory: {name!r}")
    return path


def _safe_resource(root: Path, name: str) -> Path:
    relative = PurePosixPath(name)
    if (
        not name
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise PromptBundleError(f"invalid prompt resource path: {name!r}")
    path = (root / Path(*relative.parts)).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise PromptBundleError(f"prompt resource escapes bundle: {name!r}") from exc
    return path


@dataclass(frozen=True)
class PromptBundle:
    bundle_id: str
    version: str
    assembler_version: str
    context_schema_version: str
    node_prompts: Mapping[str, str]
    digest: str
    resources: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, bundle_dir: Optional[Path] = None) -> "PromptBundle":
        root = (bundle_dir or _default_bundle_dir()).resolve()
        manifest_path = root / "bundle.json"
        if not manifest_path.is_file():
            raise PromptBundleError(f"prompt bundle manifest not found: {manifest_path}")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PromptBundleError(f"cannot read prompt bundle: {exc}") from exc

        base_keys = {
            "bundle_id",
            "version",
            "assembler_version",
            "context_schema_version",
            "nodes",
        }
        required = base_keys | {"resources"}
        if set(manifest) != required:
            raise PromptBundleError(
                "prompt bundle keys mismatch: expected %s" % sorted(required)
            )
        if not isinstance(manifest["nodes"], dict) or not manifest["nodes"]:
            raise PromptBundleError("prompt bundle nodes must be a non-empty object")

        if str(manifest["assembler_version"]) != "3":
            raise PromptBundleError("source prompt bundles must use assembler version 3")
        if str(manifest["context_schema_version"]) != "3":
            raise PromptBundleError(
                "source prompt bundles must use context schema version 3"
            )
        if str(manifest["version"]) != SUPPORTED_BUNDLE_VERSION:
            raise PromptBundleError(
                "unsupported source prompt bundle version: %s"
                % manifest["version"]
            )

        node_prompts: Dict[str, str] = {}
        for node_type, filename in sorted(manifest["nodes"].items()):
            if not isinstance(node_type, str) or not isinstance(filename, str):
                raise PromptBundleError("node prompt mapping must contain string keys and values")
            path = _safe_child(root, filename)
            try:
                prompt = path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise PromptBundleError(f"cannot read prompt for {node_type}: {exc}") from exc
            if not prompt:
                raise PromptBundleError(f"prompt for {node_type} is empty")
            node_prompts[node_type] = prompt
        resources: Dict[str, str] = {}
        manifest_resources = manifest["resources"]
        if not isinstance(manifest_resources, dict) or not manifest_resources:
            raise PromptBundleError("prompt bundle resources must be a non-empty object")
        for resource_key, filename in sorted(manifest_resources.items()):
            if not isinstance(resource_key, str) or not resource_key:
                raise PromptBundleError("prompt resource keys must be non-empty strings")
            if not isinstance(filename, str):
                raise PromptBundleError("prompt resource paths must be strings")
            path = _safe_resource(root, filename)
            try:
                content = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise PromptBundleError(
                    f"cannot read prompt resource {resource_key}: {exc}"
                ) from exc
            if not content.strip():
                raise PromptBundleError(
                    f"prompt resource {resource_key} is empty"
                )
            resources[resource_key] = content
        bundle = cls(
            bundle_id=str(manifest["bundle_id"]),
            version=str(manifest["version"]),
            assembler_version=str(manifest["assembler_version"]),
            context_schema_version=str(manifest["context_schema_version"]),
            node_prompts=node_prompts,
            digest="",
            resources=resources,
        )
        return replace(bundle, digest=bundle._snapshot_digest())

    def to_snapshot(self) -> Dict[str, Any]:
        return {
            "bundle_id": self.bundle_id,
            "version": self.version,
            "assembler_version": self.assembler_version,
            "context_schema_version": self.context_schema_version,
            "node_prompts": dict(self.node_prompts),
            "resources": dict(self.resources),
            "digest": self.digest,
        }

    @classmethod
    def from_snapshot(cls, value: Mapping[str, Any]) -> "PromptBundle":
        version = str(value.get("version", ""))
        if version != SUPPORTED_BUNDLE_VERSION:
            raise PromptBundleError(
                "unsupported prompt bundle snapshot version: %s" % version
            )
        assembler_version = str(value.get("assembler_version", ""))
        if assembler_version != "3":
            raise PromptBundleError(
                "unsupported prompt bundle snapshot assembler version: %s"
                % assembler_version
            )
        context_schema_version = str(value.get("context_schema_version", ""))
        if context_schema_version != "3":
            raise PromptBundleError(
                "unsupported prompt bundle snapshot context schema version: %s"
                % context_schema_version
            )
        expected_keys = {
            "bundle_id",
            "version",
            "assembler_version",
            "context_schema_version",
            "node_prompts",
            "resources",
            "digest",
        }
        if set(value) != expected_keys:
            raise PromptBundleError(
                "prompt bundle snapshot keys mismatch: expected %s"
                % sorted(expected_keys)
            )
        try:
            node_prompts = {
                str(k): str(v) for k, v in value["node_prompts"].items()
            }
            bundle = cls(
                bundle_id=str(value["bundle_id"]),
                version=version,
                assembler_version=assembler_version,
                context_schema_version=context_schema_version,
                node_prompts=node_prompts,
                digest=str(value["digest"]),
                resources={
                    str(k): str(v)
                    for k, v in value["resources"].items()
                },
            )
        except (KeyError, TypeError, AttributeError) as exc:
            raise PromptBundleError(f"invalid prompt bundle snapshot: {exc}") from exc
        bundle.verify_snapshot_digest()
        return bundle

    def verify_snapshot_digest(self) -> None:
        if self.digest != self._snapshot_digest():
            raise PromptBundleError("prompt bundle snapshot digest mismatch")

    def _snapshot_digest(self) -> str:
        value = {
            "bundle_id": self.bundle_id,
            "version": self.version,
            "assembler_version": self.assembler_version,
            "context_schema_version": self.context_schema_version,
            "node_prompts": dict(self.node_prompts),
            "resources": dict(self.resources),
        }
        return hashlib.sha256(_canonical_json(value)).hexdigest()
