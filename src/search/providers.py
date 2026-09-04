"""User-configurable provider registry and result normalization."""

from __future__ import annotations

import json
import os
import re
import string
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yaml
from dotenv import dotenv_values

from .contracts import SearchRequest, json_safe


_SOURCE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_ENV_REFERENCE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")
_RESULT_SHAPES = frozenset(
    {"generic", "academic", "finance", "market_cn", "annual_report_sec"}
)
_ALLOWED_SOURCE_KEYS = frozenset(
    {
        "version",
        "name",
        "script",
        "args",
        "capability",
        "query_style",
        "result_semantics",
        "result_shape",
        "item_limit_multiplier",
        "timeout_seconds",
        "max_parallel",
        "required_modules",
        "required_env",
        "optional_env",
    }
)


class ProviderRegistryError(ValueError):
    pass


@dataclass(frozen=True)
class ProviderDefinition:
    name: str
    source_file: Path
    script: str
    args: tuple[str, ...]
    capability: str
    query_style: str
    result_semantics: str
    result_shape: str = "generic"
    item_limit_multiplier: int = 1
    timeout_seconds: float = 55.0
    max_parallel: int = 2
    required_modules: tuple[str, ...] = ()
    required_credentials: tuple[str, ...] = ()
    optional_credentials: tuple[str, ...] = ()

    @property
    def environment_variables(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys((*self.required_credentials, *self.optional_credentials))
        )


def load_search_environment(
    search_dir: Path,
    *,
    profile_env_file: Optional[Path] = None,
    process_environment: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    """Load settings with process > profile > user > registry precedence."""

    from .paths import user_search_env_file

    selected: dict[str, str] = {}
    paths = [
        search_dir.expanduser().resolve() / ".env",
        user_search_env_file(),
    ]
    if profile_env_file is not None:
        paths.append(profile_env_file.expanduser().resolve())
    for path in dict.fromkeys(paths):
        if not path.is_file():
            continue
        for name, value in dotenv_values(path).items():
            if value:
                selected[str(name)] = str(value)
    selected_process_environment = (
        os.environ if process_environment is None else process_environment
    )
    for name, value in selected_process_environment.items():
        if value:
            selected[str(name)] = str(value)
    return selected


def _required_text(value: Any, *, field: str, source: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderRegistryError(
            f"search source {source.name} {field} must be non-empty text"
        )
    return value.strip()


def _string_list(value: Any, *, field: str, source: Path) -> tuple[str, ...]:
    if value is None:
        return ()
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or not item for item in value)
    ):
        raise ProviderRegistryError(
            f"search source {source.name} {field} must be a string array"
        )
    return tuple(value)


def _positive_int(
    value: Any, *, field: str, source: Path, default: int, maximum: int
) -> int:
    selected = default if value is None else value
    if (
        isinstance(selected, bool)
        or not isinstance(selected, int)
        or not 1 <= selected <= maximum
    ):
        raise ProviderRegistryError(
            f"search source {source.name} {field} must be between 1 and {maximum}"
        )
    return selected


def _positive_float(
    value: Any, *, field: str, source: Path, default: float, maximum: float
) -> float:
    selected = default if value is None else value
    if isinstance(selected, bool) or not isinstance(selected, (int, float)):
        raise ProviderRegistryError(
            f"search source {source.name} {field} must be a number"
        )
    selected = float(selected)
    if not 0 < selected <= maximum:
        raise ProviderRegistryError(
            f"search source {source.name} {field} must be greater than 0 and at most {maximum}"
        )
    return selected


def _validate_env_names(
    values: tuple[str, ...], *, field: str, source: Path
) -> tuple[str, ...]:
    invalid = [value for value in values if not _ENV_NAME.fullmatch(value)]
    if invalid:
        raise ProviderRegistryError(
            f"search source {source.name} {field} has invalid names: "
            + ", ".join(invalid)
        )
    if len(set(values)) != len(values):
        raise ProviderRegistryError(
            f"search source {source.name} {field} contains duplicate names"
        )
    return values


def _validate_templates(values: Sequence[str], *, source: Path) -> None:
    for value in values:
        try:
            fields = [
                field
                for _, field, _, _ in string.Formatter().parse(
                    _ENV_REFERENCE.sub("", value)
                )
                if field is not None
            ]
        except ValueError as exc:
            raise ProviderRegistryError(
                f"search source {source.name} has invalid template {value!r}: {exc}"
            ) from exc
        unknown = set(fields) - {"query", "limit"}
        if unknown:
            raise ProviderRegistryError(
                f"search source {source.name} has unknown template fields: "
                + ", ".join(sorted(unknown))
            )


def _load_source(source: Path) -> ProviderDefinition:
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ProviderRegistryError(
            f"cannot load search source {source.name}: {exc}"
        ) from exc
    if not isinstance(raw, Mapping):
        raise ProviderRegistryError(
            f"search source {source.name} YAML must contain an object"
        )
    value = dict(raw)
    unknown = set(value) - _ALLOWED_SOURCE_KEYS
    if unknown:
        raise ProviderRegistryError(
            f"search source {source.name} has unknown keys: "
            + ", ".join(sorted(unknown))
        )
    if str(value.get("version")) != "1":
        raise ProviderRegistryError(
            f"search source {source.name} requires version: 1"
        )
    name = _required_text(value.get("name"), field="name", source=source)
    if not _SOURCE_NAME.fullmatch(name):
        raise ProviderRegistryError(
            f"search source {source.name} has invalid name: {name!r}"
        )
    if source.stem != name:
        raise ProviderRegistryError(
            f"search source filename {source.name} must match name {name!r}"
        )
    script_path = _required_text(
        value.get("script"), field="script", source=source
    )
    if not script_path.endswith(".py"):
        raise ProviderRegistryError(
            f"search source {source.name} script must be a Python file"
        )
    args = _string_list(value.get("args"), field="args", source=source)
    _validate_templates((script_path, *args), source=source)
    required_env = _validate_env_names(
        _string_list(value.get("required_env"), field="required_env", source=source),
        field="required_env",
        source=source,
    )
    optional_env = _validate_env_names(
        _string_list(value.get("optional_env"), field="optional_env", source=source),
        field="optional_env",
        source=source,
    )
    overlap = set(required_env) & set(optional_env)
    if overlap:
        raise ProviderRegistryError(
            f"search source {source.name} repeats environment names as required and optional: "
            + ", ".join(sorted(overlap))
        )
    result_shape = value.get("result_shape", "generic")
    if result_shape not in _RESULT_SHAPES:
        raise ProviderRegistryError(
            f"search source {source.name} result_shape must be one of: "
            + ", ".join(sorted(_RESULT_SHAPES))
        )
    return ProviderDefinition(
        name=name,
        source_file=source.resolve(),
        script=script_path,
        args=args,
        capability=_required_text(
            value.get("capability"), field="capability", source=source
        ),
        query_style=_required_text(
            value.get("query_style"), field="query_style", source=source
        ),
        result_semantics=_required_text(
            value.get("result_semantics"), field="result_semantics", source=source
        ),
        result_shape=str(result_shape),
        item_limit_multiplier=_positive_int(
            value.get("item_limit_multiplier"),
            field="item_limit_multiplier",
            source=source,
            default=1,
            maximum=8,
        ),
        timeout_seconds=_positive_float(
            value.get("timeout_seconds"),
            field="timeout_seconds",
            source=source,
            default=55.0,
            maximum=600.0,
        ),
        max_parallel=_positive_int(
            value.get("max_parallel"),
            field="max_parallel",
            source=source,
            default=2,
            maximum=32,
        ),
        required_modules=_string_list(
            value.get("required_modules"), field="required_modules", source=source
        ),
        required_credentials=required_env,
        optional_credentials=optional_env,
    )


class ProviderRegistry:
    """Discover one provider definition per YAML file in a search directory."""

    def __init__(
        self,
        *,
        search_dir: Path,
        python_executable: str,
        environment: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.search_dir = search_dir.expanduser().resolve()
        if self.search_dir.is_symlink() or not self.search_dir.is_dir():
            raise ProviderRegistryError(
                f"search registry root is unavailable or unsafe: {self.search_dir}"
            )
        sources_dir = self.search_dir / "sources"
        if sources_dir.is_symlink() or not sources_dir.is_dir():
            raise ProviderRegistryError(
                f"search sources directory is unavailable or unsafe: {sources_dir}"
            )
        definitions: dict[str, ProviderDefinition] = {}
        for source in sorted(sources_dir.glob("*.yaml")):
            if source.is_symlink() or not source.is_file():
                raise ProviderRegistryError(
                    f"search source file is unsafe: {source}"
                )
            definition = _load_source(source)
            if definition.name in definitions:
                raise ProviderRegistryError(
                    f"duplicate search source name: {definition.name}"
                )
            definitions[definition.name] = definition
        if not definitions:
            raise ProviderRegistryError(
                f"no search source configurations were found in {sources_dir}"
            )
        self._definitions = definitions
        self.environment = {
            str(name): str(value)
            for name, value in (environment or {}).items()
            if value is not None
        }
        # Keep virtual-environment interpreter symlinks intact.
        self.python_executable = os.path.abspath(
            os.path.expanduser(python_executable)
        )
        self._module_checks: dict[str, bool] = {}
        self._module_check_lock = threading.Lock()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._definitions)

    @property
    def definitions(self) -> tuple[ProviderDefinition, ...]:
        return tuple(self._definitions.values())

    @property
    def environment_names(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                name
                for definition in self.definitions
                for name in definition.environment_variables
            )
        )

    @property
    def configuration_environment_names(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                match.group(1)
                for definition in self.definitions
                for value in (definition.script, *definition.args)
                for match in _ENV_REFERENCE.finditer(value)
            )
        )

    def definition(self, name: str) -> ProviderDefinition:
        try:
            return self._definitions[name]
        except KeyError as exc:
            raise ValueError(f"unsupported search provider: {name}") from exc

    def _expand_environment(self, value: str, *, definition: ProviderDefinition) -> str:
        missing: set[str] = set()

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            selected = self.environment.get(name)
            if not selected:
                missing.add(name)
                return match.group(0)
            return selected

        rendered = _ENV_REFERENCE.sub(replace, value)
        if missing:
            raise ProviderRegistryError(
                f"search source {definition.name} is missing configuration environment: "
                + ", ".join(sorted(missing))
            )
        return rendered

    def script_path(self, definition: ProviderDefinition) -> Path:
        rendered = self._expand_environment(
            definition.script, definition=definition
        )
        path = Path(rendered).expanduser()
        if path.is_absolute():
            return path.resolve()
        resolved = (self.search_dir / path).resolve()
        try:
            resolved.relative_to(self.search_dir)
        except ValueError as exc:
            raise ProviderRegistryError(
                f"search source {definition.name} script escapes search directory"
            ) from exc
        return resolved

    def command(
        self, request: SearchRequest, *, limit: int
    ) -> tuple[ProviderDefinition, list[str]]:
        definition = self.definition(request.provider)
        rendered_args = [
            self._expand_environment(value, definition=definition).format(
                query=request.query,
                limit=limit,
            )
            for value in definition.args
        ]
        return definition, [
            self.python_executable,
            str(self.script_path(definition)),
            *rendered_args,
        ]

    def missing_modules(
        self,
        definition: ProviderDefinition,
        *,
        timeout_seconds: float = 10.0,
    ) -> tuple[str, ...]:
        missing: list[str] = []
        for module in definition.required_modules:
            with self._module_check_lock:
                available = self._module_checks.get(module)
            if available is None:
                completed = False
                try:
                    check = subprocess.run(
                        [
                            self.python_executable,
                            "-c",
                            (
                                "import importlib.util,sys;"
                                f"sys.exit(0 if importlib.util.find_spec({module!r}) else 1)"
                            ),
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=max(0.01, timeout_seconds),
                        check=False,
                    )
                    available = check.returncode == 0
                    completed = True
                except OSError:
                    available = False
                    completed = True
                except subprocess.TimeoutExpired:
                    available = False
                if completed:
                    with self._module_check_lock:
                        available = self._module_checks.setdefault(
                            module, available
                        )
            if not available:
                missing.append(module)
        return tuple(missing)

    def availability(
        self,
        definition: ProviderDefinition,
        *,
        module_check_timeout_seconds: float = 10.0,
    ) -> tuple[bool, Optional[str]]:
        try:
            script = self.script_path(definition)
        except ProviderRegistryError as exc:
            return False, str(exc)
        if not script.is_file():
            return False, f"missing script: {definition.script}"
        missing_credentials = tuple(
            name
            for name in definition.required_credentials
            if not self.environment.get(name) and not os.environ.get(name)
        )
        if missing_credentials:
            return False, (
                "missing required credential environment: "
                + ", ".join(missing_credentials)
            )
        missing = self.missing_modules(
            definition, timeout_seconds=module_check_timeout_seconds
        )
        if missing:
            return False, (
                "provider Python is missing modules: " + ", ".join(missing)
            )
        return True, None

    def list_sources(self) -> list[dict[str, Any]]:
        sources: list[dict[str, Any]] = []
        for definition in self.definitions:
            available, unavailable_reason = self.availability(definition)
            sources.append(
                {
                    "provider": definition.name,
                    "source_file": definition.source_file.name,
                    "available": available,
                    "unavailable_reason": unavailable_reason,
                    "capability": definition.capability,
                    "query_style": definition.query_style,
                    "result_semantics": definition.result_semantics,
                    "required_credentials": list(
                        definition.required_credentials
                    ),
                    "optional_credentials": list(
                        definition.optional_credentials
                    ),
                }
            )
        return sources

    def describe(self, name: str) -> dict[str, Any]:
        definition = self.definition(name)
        available, unavailable_reason = self.availability(definition)
        return {
            "provider": definition.name,
            "source_file": definition.source_file.name,
            "script": definition.script,
            "args": list(definition.args),
            "capability": definition.capability,
            "query_style": definition.query_style,
            "result_semantics": definition.result_semantics,
            "result_shape": definition.result_shape,
            "item_limit_multiplier": definition.item_limit_multiplier,
            "timeout_seconds": definition.timeout_seconds,
            "max_parallel": definition.max_parallel,
            "required_modules": list(definition.required_modules),
            "required_env": list(definition.required_credentials),
            "optional_env": list(definition.optional_credentials),
            "available": available,
            "unavailable_reason": unavailable_reason,
        }


def parse_json_output(text: str) -> Any:
    stripped = text.strip()
    if not stripped:
        raise ValueError("provider returned empty stdout")
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as original_error:
        # Some existing scripts or their dependencies write diagnostics such
        # as ``[INFO] ...`` before the JSON document. Restrict fallback probes
        # to a bounded number of line starts, working backwards from the end
        # where these scripts emit their final result. Probing every ``{`` or
        # ``[`` lets malformed output turn parsing into quadratic work.
        decoder = json.JSONDecoder()
        candidates = [
            match.start(1)
            for match in re.finditer(r"(?m)^[ \t]*([\[{])", stripped)
        ]
        for start in reversed(candidates[-16:]):
            try:
                value, _ = decoder.raw_decode(stripped, start)
            except json.JSONDecodeError:
                continue
            return value
        raise original_error


def _nested_items(
    payload: Mapping[str, Any], provider: str, result_shape: str
) -> list[tuple[str, dict[str, Any]]]:
    if result_shape == "academic":
        values: list[tuple[str, dict[str, Any]]] = []
        source_results = payload.get("source_results")
        if isinstance(source_results, Sequence) and not isinstance(source_results, str):
            for source_result in source_results:
                if not isinstance(source_result, Mapping):
                    continue
                logical_source = str(source_result.get("source") or "unknown")
                fallback_provider = source_result.get("provider")
                items = source_result.get("items")
                if isinstance(items, Sequence) and not isinstance(items, str):
                    for item in items:
                        if not isinstance(item, Mapping):
                            continue
                        copied = dict(item)
                        actual_provider = (
                            copied.get("provider")
                            or fallback_provider
                            or logical_source
                        )
                        copied["_academic_source"] = logical_source
                        copied["_academic_provider"] = actual_provider
                        values.append((f"academic:{actual_provider}", copied))
        return values

    containers: list[tuple[str, Any]] = [(provider, payload.get("items"))]
    data = payload.get("data")
    if isinstance(data, Mapping):
        for key in (
            "items",
            "announcements",
            "quotes",
            "news",
            "research",
            "nav",
            "lists",
            "filings",
            "results",
        ):
            containers.append((provider, (key, data.get(key))))
    elif isinstance(data, Sequence) and not isinstance(data, str):
        containers.append((provider, data))
    for key in ("results", "announcements", "filings", "videos", "posts"):
        containers.append((provider, payload.get(key)))

    output: list[tuple[str, dict[str, Any]]] = []
    seen_objects: set[int] = set()
    for route, values in containers:
        result_kind: Optional[str] = None
        if (
            isinstance(values, tuple)
            and len(values) == 2
            and isinstance(values[0], str)
        ):
            result_kind, values = values
        if isinstance(values, Mapping):
            values = [values]
        if not isinstance(values, Sequence) or isinstance(values, str):
            continue
        for item in values:
            if not isinstance(item, Mapping) or id(item) in seen_objects:
                continue
            seen_objects.add(id(item))
            copied = dict(item)
            if result_shape == "finance" and isinstance(item.get("content"), Mapping):
                copied = {**dict(item["content"]), **copied}
            if result_kind is not None:
                copied["_result_kind"] = result_kind
            if result_shape == "annual_report_sec" and isinstance(data, Mapping):
                copied["entity_name"] = data.get("entity_name")
                copied["cik"] = data.get("cik")
                copied["tickers"] = data.get("tickers")
            output.append((route, copied))
    return output


_TITLE_KEYS = (
    "title",
    "name",
    "announcementTitle",
    "announcement_title",
    "shortname",
    "longname",
    "symbol",
    "form",
    "company_name",
)
_URL_KEYS = (
    "url",
    "html_url",
    "pdf_url",
    "pdfUrl",
    "document_url",
    "filing_url",
    "filing_directory_url",
    "hn_url",
    "webUrl",
    "link",
    "external_url",
)
_SNIPPET_KEYS = (
    "abstract",
    "snippet",
    "description",
    "body",
    "text",
    "summary",
    "content",
)
_METADATA_KEYS = frozenset(
    {
        # Academic identity, provenance, date, and impact fields.
        "year",
        "venue",
        "publication_date",
        "publication_venue",
        "citation_count",
        "citationCount",
        "influential_citation_count",
        "reference_count",
        "is_open_access",
        "open_access_pdf",
        "fields_of_study",
        "publication_types",
        "tldr",
        "source",
        "provider_source",
        "provider_rating",
        "stars",
        "language",
        "score",
        "comments",
        "answer_count",
        "is_answered",
        "accepted_answer_id",
        "creation_date",
        "created_at",
        "updated_at",
        "published_at",
        "publishedAt",
        "pubdate",
        "created_utc",
        "create_time",
        "last_modified",
        "subreddit",
        "downloads",
        "likes",
        "like",
        "doi",
        "arxiv_id",
        "paper_id",
        "paperId",
        "openalex_id",
        "scholar_id",
        "pmid",
        "pmc_id",
        "page_id",
        "authors",
        "categories",
        "pdf_url",
        "tags",
        "ticker",
        "symbol",
        "exchange",
        "quoteType",
        "typeDisp",
        "exchDisp",
        "sector",
        "industry",
        "publisher",
        "provider",
        "providerPublishTime",
        "pubDate",
        "displayTime",
        "contentType",
        "isHosted",
        "form",
        "filing_date",
        "report_date",
        "accession_number",
        "primary_document",
        "document_url",
        "filing_directory_url",
        "entity_name",
        "cik",
        "tickers",
        "announcementTime",
        "announcementId",
        "adjunctSize",
        "adjunctType",
        "announcementType",
        "announcement_date",
        "announcement_id",
        "adjunct_size_kb",
        "adjunct_type",
        "raw_category",
        "secCode",
        "secName",
        "points",
        "num_comments",
        "author",
        "hn_url",
        "channel",
        "screen_name",
        "content_type",
        "content_file",
        "author_headline",
        "author_followers",
        "voteup_count",
        "favorite_count",
        "favorites_count",
        "retweet_count",
        "comment_count",
        "share_count",
        "play_count",
        "visits_count",
        "play",
        "view",
        "digg_count",
        "question_title",
        "question_url",
        "thumbnail",
        "pipeline_tag",
        "library",
        "sdk",
        "state",
        "type",
        "external_url",
        "_result_kind",
        "_academic_source",
        "_academic_provider",
        "sec_code",
        "sec_name",
    }
)


_MAX_RAW_FIELDS = 80
_MAX_RAW_ARRAY_ITEMS = 80
_MAX_RAW_STRING_CHARS = 4_000
_MAX_RAW_DEPTH = 8
_MAX_RAW_SERIALIZED_CHARS = 20_000


def _first_text(item: Mapping[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, (Mapping, list, tuple, set)):
            continue
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _nested_url(item: Mapping[str, Any]) -> str:
    direct = _first_text(item, _URL_KEYS)
    if direct and not direct.startswith("{"):
        return direct
    for key in ("canonicalUrl", "clickThroughUrl"):
        value = item.get(key)
        if isinstance(value, Mapping):
            nested = value.get("url")
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    content = item.get("content")
    if isinstance(content, Mapping):
        return _nested_url(content)
    return ""


def _bounded_json_value(
    value: Any,
    *,
    max_fields: int,
    max_array_items: int,
    max_string_chars: int,
    max_depth: int,
    depth: int = 0,
) -> tuple[Any, bool]:
    """Return JSON-compatible data plus whether any content was omitted.

    Unlike :func:`json_safe`, this helper reports truncation while it happens,
    so ``raw_item_truncated`` cannot be accidentally computed from an already
    shortened value.
    """

    if value is None or isinstance(value, (bool, int, float)):
        return value, False
    if isinstance(value, str):
        if len(value) <= max_string_chars:
            return value, False
        return value[: max(0, max_string_chars - 1)] + "…", True
    if depth >= max_depth:
        if isinstance(value, Mapping):
            return "[nested object truncated]", True
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            return "[nested array truncated]", True

    if isinstance(value, Mapping):
        raw_items = list(value.items())
        truncated = len(raw_items) > max_fields
        output: dict[str, Any] = {}
        for raw_key, item in raw_items[:max_fields]:
            key = str(raw_key)
            if len(key) > 200:
                key = key[:199] + "…"
                truncated = True
            bounded, item_truncated = _bounded_json_value(
                item,
                max_fields=max_fields,
                max_array_items=max_array_items,
                max_string_chars=max_string_chars,
                max_depth=max_depth,
                depth=depth + 1,
            )
            output[key] = bounded
            truncated = truncated or item_truncated
        return output, truncated

    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        raw_items = list(value)
        truncated = len(raw_items) > max_array_items
        output: list[Any] = []
        for item in raw_items[:max_array_items]:
            bounded, item_truncated = _bounded_json_value(
                item,
                max_fields=max_fields,
                max_array_items=max_array_items,
                max_string_chars=max_string_chars,
                max_depth=max_depth,
                depth=depth + 1,
            )
            output.append(bounded)
            truncated = truncated or item_truncated
        return output, truncated

    text = str(value)
    if len(text) > max_string_chars:
        return text[: max(0, max_string_chars - 1)] + "…", True
    return text, False


def _bounded_raw_item(item: Mapping[str, Any]) -> tuple[Any, bool]:
    raw_item, truncated = _bounded_json_value(
        item,
        max_fields=_MAX_RAW_FIELDS,
        max_array_items=_MAX_RAW_ARRAY_ITEMS,
        max_string_chars=_MAX_RAW_STRING_CHARS,
        max_depth=_MAX_RAW_DEPTH,
    )
    raw_text = json.dumps(raw_item, ensure_ascii=False, separators=(",", ":"))
    if len(raw_text) <= _MAX_RAW_SERIALIZED_CHARS:
        return raw_item, truncated

    # A total-size ceiling is needed in addition to the per-value limits: a
    # wide nested record could otherwise remain much larger than one MCP hit.
    compact, _ = _bounded_json_value(
        item,
        max_fields=12,
        max_array_items=8,
        max_string_chars=512,
        max_depth=3,
    )
    compact_text = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    if len(compact_text) <= _MAX_RAW_SERIALIZED_CHARS:
        return compact, True

    # Pathological nesting still gets a bounded, inspectable preview instead
    # of an unbounded object.  The source URL remains available on the hit.
    return {
        "_preview": compact_text[: _MAX_RAW_STRING_CHARS - 1] + "…",
        "_original_type": type(item).__name__,
    }, True


def normalize_search_hits(
    *,
    request: SearchRequest,
    payload: Any,
    max_items: Optional[int] = None,
    result_shape: str = "generic",
) -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping):
        return []
    hits: list[dict[str, Any]] = []
    if result_shape not in _RESULT_SHAPES:
        raise ValueError(f"unsupported search result shape: {result_shape}")
    for route, item in _nested_items(payload, request.provider, result_shape):
        title = _first_text(item, _TITLE_KEYS)
        url = _nested_url(item)
        snippet = _first_text(item, _SNIPPET_KEYS)
        if result_shape == "finance":
            symbol = str(item.get("symbol") or "").strip()
            if not title:
                title = symbol
            if not url and symbol:
                url = f"https://finance.yahoo.com/quote/{symbol}"
        elif result_shape == "annual_report_sec":
            # ``form`` is one of the generic title candidates, but "10-K" by
            # itself is not useful in a mixed result set.  SEC filing titles
            # always carry entity, form, and the best available date.
            entity = str(item.get("entity_name") or request.query).strip()
            form = str(item.get("form") or "SEC filing").strip()
            report_date = str(
                item.get("report_date") or item.get("filing_date") or ""
            ).strip()
            title = " ".join(part for part in (entity, form, report_date) if part)
        elif result_shape == "market_cn" and not snippet:
            snippet = " ".join(
                str(item.get(key) or "").strip()
                for key in ("secName", "secCode", "announcementTime")
                if str(item.get(key) or "").strip()
            )
        if not title and not url:
            continue
        metadata = {
            # json_safe appends one ellipsis character when truncating, hence
            # the one-character adjustment preserves the hard 4k ceiling.
            key: json_safe(value, max_string_chars=_MAX_RAW_STRING_CHARS - 1)
            for key, value in item.items()
            if key in _METADATA_KEYS and value not in (None, "", [], {})
        }
        raw_item, raw_item_truncated = _bounded_raw_item(item)
        hits.append(
            {
                "source_provider": request.provider,
                "provider": route,
                "query": request.query,
                "evidence_target": request.evidence_target,
                "intent": request.intent,
                "title": title[:_MAX_RAW_STRING_CHARS],
                "url": url[:_MAX_RAW_STRING_CHARS] or None,
                "snippet": snippet[:4_000],
                "metadata": metadata,
                "raw_item": raw_item,
                "raw_item_truncated": raw_item_truncated,
            }
        )
        if max_items is not None and len(hits) >= max_items:
            break
    return hits


def _normalize_identifier(value: object) -> str:
    text = str(value or "").strip().casefold()
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text)
    text = re.sub(r"^doi:\s*", "", text)
    return text.rstrip("/")


def _normalize_arxiv_identifier(value: object) -> str:
    text = str(value or "").strip().casefold()
    text = re.sub(r"^arxiv:\s*", "", text)
    text = re.sub(r"^https?://(?:export\.)?arxiv\.org/(?:abs|pdf)/", "", text)
    text = text.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    text = text.removesuffix(".pdf")
    # arXiv revisions identify versions of the same paper, not independent
    # search hits (e.g. 2401.01234v1 and 2401.01234v3).
    return re.sub(r"v\d+$", "", text)


def canonical_url(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return text.casefold().rstrip("/")
    if not parts.netloc:
        return text.casefold().rstrip("/")
    query = urlencode(
        sorted(
            (key, val)
            for key, val in parse_qsl(parts.query, keep_blank_values=True)
            if not key.casefold().startswith("utm_")
            and key.casefold() not in {"ref", "source", "fbclid", "gclid"}
        )
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(
        (parts.scheme.casefold() or "https", parts.netloc.casefold(), path, query, "")
    )


def canonical_hit_keys(hit: Mapping[str, Any]) -> tuple[str, ...]:
    metadata = hit.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    keys: list[str] = []
    for field in ("doi", "arxiv_id", "paper_id", "paperId"):
        if field == "arxiv_id":
            identifier = _normalize_arxiv_identifier(metadata.get(field))
        else:
            identifier = _normalize_identifier(metadata.get(field))
        if identifier:
            family = "paper_id" if field in {"paper_id", "paperId"} else field
            keys.append(f"{family}:{identifier}")
    url = canonical_url(hit.get("url"))
    if url:
        keys.append(f"url:{url}")
        arxiv_match = re.search(r"arxiv\.org/(?:abs|pdf)/([^/?#]+)", url)
        if arxiv_match:
            arxiv_id = _normalize_arxiv_identifier(arxiv_match.group(1))
            if arxiv_id:
                keys.append(f"arxiv_id:{arxiv_id}")
    title = re.sub(r"[^\w]+", " ", str(hit.get("title") or "").casefold()).strip()
    # Titles are deliberately fallback-only.  Papers can legitimately share a
    # title; merging two records that each have a different DOI loses evidence.
    if not keys and title:
        if str(hit.get("source_provider")) == "academic":
            keys.append(f"academic_title:{title}")
        else:
            keys.append(f"provider_title:{hit.get('source_provider')}:{title}")
    return tuple(dict.fromkeys(keys))


def provider_payload_warnings(payload: Any) -> list[dict[str, Any]]:
    """Expose partial source failures without flattening them into one string.

    The academic wrapper can succeed overall while one selected logical source
    fails.  Keeping source/provider/attempts separate lets the service show a
    useful warning and still retain hits from the healthy source.
    """

    if not isinstance(payload, Mapping):
        return []
    source_results = payload.get("source_results")
    if not isinstance(source_results, Sequence) or isinstance(
        source_results, (str, bytes, bytearray)
    ):
        return []

    warnings: list[dict[str, Any]] = []
    for result in source_results:
        if not isinstance(result, Mapping):
            continue
        error = result.get("error")
        if result.get("success") is not False and not error:
            continue
        warnings.append(
            {
                "code": "provider_source_failed",
                "source": json_safe(result.get("source")),
                "provider": json_safe(result.get("provider")),
                "error": json_safe(error or "source failed"),
                "attempts": json_safe(result.get("attempts") or []),
            }
        )
    return warnings


def provider_payload_error(payload: Any) -> Optional[Any]:
    if not isinstance(payload, Mapping):
        return None
    return payload.get("error") or payload.get("errors")
