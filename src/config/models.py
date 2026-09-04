from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Dict, Mapping, Optional, Tuple


_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_PORT = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


class SpecValidationError(ValueError):
    pass


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SpecValidationError(f"{label} must be non-empty text")
    return value.strip()


def _identifier(value: Any, label: str) -> str:
    result = _text(value, label)
    if not _ID.fullmatch(result):
        raise SpecValidationError(f"{label} must match {_ID.pattern!r}")
    return result


def _port_name(value: Any, label: str) -> str:
    result = _text(value, label)
    if not _PORT.fullmatch(result):
        raise SpecValidationError(f"{label} must match {_PORT.pattern!r}")
    return result


def _plain_mapping(value: Any, label: str) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SpecValidationError(f"{label} must be an object")
    return copy.deepcopy(dict(value))


@dataclass(frozen=True)
class RunRequest:
    query: str
    language: str = "zh-CN"
    mode: str = "normal"
    output_format: str = "markdown"
    report_format: str = "formal_report"

    def __post_init__(self) -> None:
        object.__setattr__(self, "query", _text(self.query, "query"))
        object.__setattr__(self, "language", _text(self.language, "language"))
        object.__setattr__(self, "mode", _identifier(self.mode, "mode"))
        normalized = _text(self.output_format, "output_format").lower()
        if normalized == "report":
            normalized = "markdown"
        if normalized not in {"markdown", "html", "pdf", "docx"}:
            raise SpecValidationError(
                "output_format must be markdown, html, pdf, or docx"
            )
        object.__setattr__(self, "output_format", normalized)
        report_format = _text(self.report_format, "report_format").lower()
        if report_format not in {"brief", "formal_report"}:
            raise SpecValidationError(
                "report_format must be brief or formal_report"
            )
        object.__setattr__(self, "report_format", report_format)

    def to_dict(self) -> Dict[str, str]:
        return {
            "query": self.query,
            "language": self.language,
            "mode": self.mode,
            "output_format": self.output_format,
            "report_format": self.report_format,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RunRequest":
        return cls(
            query=value.get("query"),
            language=value.get("language", "zh-CN"),
            mode=value.get("mode", "normal"),
            output_format=value.get("output_format", "markdown"),
            report_format=(
                "formal_report"
                if value.get("report_format") == "standard_report"
                else value.get("report_format", "formal_report")
            ),
        )


@dataclass(frozen=True)
class InputPort:
    name: str
    artifact_type: str
    media_types: Tuple[str, ...]
    mode: str
    required: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _port_name(self.name, "input port"))
        object.__setattr__(
            self, "artifact_type", _identifier(self.artifact_type, "artifact type")
        )
        media = tuple(_text(item, "media type") for item in self.media_types)
        if not media:
            raise SpecValidationError(f"input {self.name} requires media_type")
        object.__setattr__(self, "media_types", media)
        if self.mode not in {"one", "all", "each"}:
            raise SpecValidationError(f"input {self.name} mode must be one, all, or each")
        if not isinstance(self.required, bool):
            raise SpecValidationError(f"input {self.name} required must be boolean")

    def accepts(self, output: "OutputPort") -> bool:
        return (
            self.artifact_type == output.artifact_type
            and output.media_type in self.media_types
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.artifact_type,
            "media_type": list(self.media_types),
            "mode": self.mode,
            "required": self.required,
        }

    @classmethod
    def from_dict(cls, name: str, value: Mapping[str, Any]) -> "InputPort":
        media = value.get("media_type")
        media_types = tuple(media) if isinstance(media, list) else (media,)
        return cls(
            name=name,
            artifact_type=value.get("type"),
            media_types=media_types,
            mode=value.get("mode", "one"),
            required=value.get("required", True),
        )


@dataclass(frozen=True)
class OutputPort:
    name: str
    path: str
    artifact_type: str
    media_type: str
    mode: str = "state"
    primary: bool = False
    required: bool = True
    scope_key: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _port_name(self.name, "output port"))
        object.__setattr__(self, "path", _text(self.path, f"output {self.name} path"))
        relative = PurePosixPath(self.path)
        if (
            relative.is_absolute()
            or "\\" in self.path
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise SpecValidationError(f"output {self.name} path must be safe and relative")
        object.__setattr__(
            self, "artifact_type", _identifier(self.artifact_type, "artifact type")
        )
        object.__setattr__(self, "media_type", _text(self.media_type, "media type"))
        if self.mode not in {"state", "batch"}:
            raise SpecValidationError(f"output {self.name} mode must be state or batch")
        if not isinstance(self.primary, bool) or not isinstance(self.required, bool):
            raise SpecValidationError(
                f"output {self.name} primary and required must be boolean"
            )
        if self.mode == "batch":
            if "*" not in self.path:
                raise SpecValidationError(f"batch output {self.name} path must contain '*'")
            object.__setattr__(
                self,
                "scope_key",
                _identifier(self.scope_key or "item-id", f"output {self.name} scope_key"),
            )
        elif "*" in self.path:
            raise SpecValidationError(f"state output {self.name} cannot use a glob path")

    def to_dict(self) -> Dict[str, Any]:
        value: Dict[str, Any] = {
            "path": self.path,
            "type": self.artifact_type,
            "media_type": self.media_type,
            "mode": self.mode,
            "primary": self.primary,
            "required": self.required,
        }
        if self.scope_key:
            value["scope_key"] = self.scope_key
        return value

    @classmethod
    def from_dict(cls, name: str, value: Mapping[str, Any]) -> "OutputPort":
        return cls(
            name=name,
            path=value.get("path"),
            artifact_type=value.get("type"),
            media_type=value.get("media_type"),
            mode=value.get("mode", "state"),
            primary=value.get("primary", False),
            required=value.get("required", True),
            scope_key=value.get("scope_key"),
        )


@dataclass(frozen=True)
class NodeSpec:
    node_id: str
    kind: str
    inputs: Tuple[InputPort, ...]
    outputs: Tuple[OutputPort, ...]
    prompt: Optional[str] = None
    command: Tuple[str, ...] = ()
    preparer: Tuple[str, ...] = ()
    prepared_outputs: Tuple[str, ...] = ()
    materializer: Tuple[str, ...] = ()
    validator: Tuple[str, ...] = ()
    validators: Tuple[Tuple[str, ...], ...] = ()
    resources: Mapping[str, str] = field(default_factory=dict)
    repair_on_validation_failure: bool = True
    version: str = "1"

    def __post_init__(self) -> None:
        if self.version != "1":
            raise SpecValidationError("node version must be 1")
        object.__setattr__(self, "node_id", _identifier(self.node_id, "node id"))
        if self.kind not in {"agent", "script"}:
            raise SpecValidationError("node kind must be agent or script")
        object.__setattr__(self, "inputs", tuple(self.inputs))
        object.__setattr__(self, "outputs", tuple(self.outputs))
        if not self.outputs:
            raise SpecValidationError(f"node {self.node_id} must declare outputs")
        for collection, label in ((self.inputs, "input"), (self.outputs, "output")):
            names = [item.name for item in collection]
            if len(names) != len(set(names)):
                raise SpecValidationError(f"node {self.node_id} has duplicate {label} ports")
        primary_outputs = [item for item in self.outputs if item.primary]
        if len(primary_outputs) > 1:
            raise SpecValidationError(
                f"node {self.node_id} cannot declare multiple primary outputs"
            )
        if primary_outputs and primary_outputs[0].mode != "state":
            raise SpecValidationError(
                f"node {self.node_id} primary output must use state mode"
            )
        if self.kind == "agent":
            if not self.prompt:
                raise SpecValidationError(f"agent node {self.node_id} requires prompt")
            if self.command:
                raise SpecValidationError(f"agent node {self.node_id} cannot define command")
        else:
            if not self.command:
                raise SpecValidationError(f"script node {self.node_id} requires command")
            if self.prompt:
                raise SpecValidationError(f"script node {self.node_id} cannot define prompt")
        object.__setattr__(self, "preparer", tuple(self.preparer))
        object.__setattr__(self, "prepared_outputs", tuple(self.prepared_outputs))
        object.__setattr__(self, "materializer", tuple(self.materializer))
        object.__setattr__(self, "validators", tuple(tuple(item) for item in self.validators))
        if any(not isinstance(item, str) or not item for item in self.preparer):
            raise SpecValidationError("preparer must be a non-empty string array")
        output_names = {item.name for item in self.outputs}
        if len(self.prepared_outputs) != len(set(self.prepared_outputs)) or any(
            item not in output_names for item in self.prepared_outputs
        ):
            raise SpecValidationError("prepared_outputs must name unique declared outputs")
        if self.prepared_outputs and not self.preparer:
            raise SpecValidationError("prepared_outputs requires a preparer")
        if self.validator and self.validators:
            raise SpecValidationError("node cannot define both validator and validators")
        if any(not isinstance(item, str) or not item for item in self.materializer):
            raise SpecValidationError("materializer must be a non-empty string array")
        if any(not command or any(not isinstance(item, str) or not item for item in command) for command in self.validators):
            raise SpecValidationError("validators must contain non-empty string arrays")
        object.__setattr__(self, "resources", dict(self.resources))
        if not isinstance(self.repair_on_validation_failure, bool):
            raise SpecValidationError("repair_on_validation_failure must be boolean")

    def input(self, name: str) -> InputPort:
        return next(item for item in self.inputs if item.name == name)

    def output(self, name: str) -> OutputPort:
        return next(item for item in self.outputs if item.name == name)

    def to_dict(self) -> Dict[str, Any]:
        value: Dict[str, Any] = {
            "version": self.version,
            "id": self.node_id,
            "kind": self.kind,
            "inputs": {item.name: item.to_dict() for item in self.inputs},
            "outputs": {item.name: item.to_dict() for item in self.outputs},
            "command": list(self.command),
            "preparer": list(self.preparer),
            "prepared_outputs": list(self.prepared_outputs),
            "materializer": list(self.materializer),
            "validator": list(self.validator),
            "validators": [list(item) for item in self.validators],
            "resources": dict(self.resources),
            "repair_on_validation_failure": self.repair_on_validation_failure,
        }
        if self.prompt is not None:
            value["prompt"] = self.prompt
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "NodeSpec":
        inputs = _plain_mapping(value.get("inputs", {}), "node inputs")
        outputs = _plain_mapping(value.get("outputs", {}), "node outputs")
        return cls(
            version=str(value.get("version", "1")),
            node_id=value.get("id"),
            kind=value.get("kind"),
            inputs=tuple(InputPort.from_dict(name, spec) for name, spec in inputs.items()),
            outputs=tuple(OutputPort.from_dict(name, spec) for name, spec in outputs.items()),
            prompt=value.get("prompt"),
            command=tuple(value.get("command", ())),
            preparer=tuple(value.get("preparer", ())),
            prepared_outputs=tuple(value.get("prepared_outputs", ())),
            materializer=tuple(value.get("materializer", ())),
            validator=tuple(value.get("validator", ())),
            validators=tuple(tuple(item) for item in value.get("validators", ())),
            resources=value.get("resources", {}),
            repair_on_validation_failure=value.get("repair_on_validation_failure", True),
        )


@dataclass(frozen=True)
class WorkflowSpec:
    name: str
    steps: Tuple[str, ...]
    result: str
    version: str = "1"
    timeouts: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.version != "1":
            raise SpecValidationError("workflow version must be 1")
        object.__setattr__(self, "name", _identifier(self.name, "workflow name"))
        object.__setattr__(self, "steps", tuple(_identifier(x, "workflow step") for x in self.steps))
        if not self.steps:
            raise SpecValidationError("workflow steps must not be empty")
        object.__setattr__(self, "result", _identifier(self.result, "workflow result"))
        normalized_timeouts: Dict[str, float] = {}
        for key, raw_value in _plain_mapping(
            self.timeouts, "workflow timeouts"
        ).items():
            timeout_key = _identifier(key, "workflow timeout key")
            if (
                isinstance(raw_value, bool)
                or not isinstance(raw_value, (int, float))
                or not math.isfinite(raw_value)
                or raw_value <= 0
            ):
                raise SpecValidationError(
                    f"workflow timeout {timeout_key} must be finite and positive"
                )
            normalized_timeouts[timeout_key] = float(raw_value)
        object.__setattr__(self, "timeouts", normalized_timeouts)

    def to_dict(self) -> Dict[str, Any]:
        value = {
            "version": self.version,
            "name": self.name,
            "steps": list(self.steps),
            "result": self.result,
        }
        if self.timeouts:
            value["timeouts"] = dict(self.timeouts)
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkflowSpec":
        raw_steps = value.get("steps")
        if not isinstance(raw_steps, list):
            raise SpecValidationError("workflow steps must be a list")
        return cls(
            version=str(value.get("version", "1")),
            name=value.get("name"),
            steps=tuple(raw_steps),
            result=value.get("result"),
            timeouts=value.get("timeouts", {}),
        )


@dataclass(frozen=True)
class InputBinding:
    port: str
    mode: str
    source_step_id: Optional[str]
    artifact_type: str
    required: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "port": self.port,
            "mode": self.mode,
            "source_step_id": self.source_step_id,
            "artifact_type": self.artifact_type,
            "required": self.required,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "InputBinding":
        return cls(
            port=value.get("port"),
            mode=value.get("mode"),
            source_step_id=value.get("source_step_id"),
            artifact_type=value.get("artifact_type"),
            required=value.get("required", True),
        )


@dataclass(frozen=True)
class CompiledStep:
    step_id: str
    node_id: str
    bindings: Tuple[InputBinding, ...]
    timeout_seconds: Optional[float] = None

    def __post_init__(self) -> None:
        if self.timeout_seconds is not None and (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise SpecValidationError(
                "compiled step timeout_seconds must be finite and positive"
            )
        if self.timeout_seconds is not None:
            object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))

    def to_dict(self) -> Dict[str, Any]:
        value = {
            "id": self.step_id,
            "use": self.node_id,
            "bindings": [item.to_dict() for item in self.bindings],
        }
        if self.timeout_seconds is not None:
            value["timeout_seconds"] = self.timeout_seconds
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CompiledStep":
        return cls(
            step_id=value.get("id"),
            node_id=value.get("use"),
            bindings=tuple(InputBinding.from_dict(item) for item in value.get("bindings", ())),
            timeout_seconds=value.get("timeout_seconds"),
        )


@dataclass(frozen=True)
class CompiledWorkflow:
    name: str
    steps: Tuple[CompiledStep, ...]
    result_type: str
    result_media_type: str
    output_format: str
    version: str = "1"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "name": self.name,
            "steps": [item.to_dict() for item in self.steps],
            "result_type": self.result_type,
            "result_media_type": self.result_media_type,
            "output_format": self.output_format,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CompiledWorkflow":
        if str(value.get("version", "")) != "1":
            raise SpecValidationError("compiled workflow version must be 1")
        return cls(
            version=str(value.get("version", "1")),
            name=value.get("name"),
            steps=tuple(CompiledStep.from_dict(item) for item in value.get("steps", ())),
            result_type=value.get("result_type"),
            result_media_type=value.get("result_media_type"),
            output_format=value.get("output_format"),
        )


@dataclass(frozen=True)
class Artifact:
    port: str
    artifact_type: str
    media_type: str
    path: str
    sha256: str
    scope: Mapping[str, str]
    mode: str
    step_id: str
    instance_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", dict(self.scope))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "port": self.port,
            "type": self.artifact_type,
            "media_type": self.media_type,
            "path": self.path,
            "sha256": self.sha256,
            "scope": dict(self.scope),
            "mode": self.mode,
            "step_id": self.step_id,
            "instance_id": self.instance_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Artifact":
        return cls(
            port=value.get("port"),
            artifact_type=value.get("type"),
            media_type=value.get("media_type"),
            path=value.get("path"),
            sha256=value.get("sha256"),
            scope=value.get("scope", {}),
            mode=value.get("mode"),
            step_id=value.get("step_id"),
            instance_id=value.get("instance_id"),
        )
