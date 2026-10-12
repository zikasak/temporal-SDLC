"""Tools the API designer uses to keep the API model current.

Not part of the chat TOOLSETS: the API design activity builds these itself.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Annotated, Any, NotRequired, TypedDict

from langchain_core.tools import BaseTool, tool
from pydantic import BeforeValidator

from app.api_design.models import (
    AUTH_SCHEMES,
    HTTP_METHODS,
    PARAMETER_LOCATIONS,
    PRIMITIVE_TYPES,
    ApiInfo,
    ApiModel,
    Endpoint,
    Parameter,
    Property,
    Schema,
)
from app.api_design.openapi import PATH_PATTERN, design_problems, endpoint_key
from app.languages import get_language

logger = logging.getLogger(__name__)

SCHEMA_NAME = re.compile(r"[A-Z][A-Za-z0-9]*")
FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
HEADER_NAME = re.compile(r"[A-Za-z][A-Za-z0-9-]*")
ARRAY_TYPE = re.compile(r"(?:array|list)\s*[<\[]\s*(.+?)\s*[>\]]|(.+?)\s*\[\]", re.IGNORECASE)
TYPE_ALIASES = {
    "str": "string", "text": "string", "int": "integer", "long": "integer",
    "float": "number", "double": "number", "decimal": "number", "bool": "boolean",
    "dict": "object", "map": "object", "json": "object",
}
DEFAULT_STATUSES = {"POST": 201, "DELETE": 204}


class PropertyInput(TypedDict):
    name: str
    type: str
    required: NotRequired[bool]
    array: NotRequired[bool]
    format: NotRequired[str]
    enum: NotRequired[list[str]]
    description: NotRequired[str]


class ParameterInput(TypedDict):
    name: str
    location: NotRequired[str]
    type: NotRequired[str]
    required: NotRequired[bool]
    description: NotRequired[str]


def parse_type(value: str) -> tuple[str, bool]:
    """A primitive or schema name, and whether it is a list (`Room[]`, `list[Room]`)."""
    text = value.strip()
    array = ARRAY_TYPE.fullmatch(text)
    if array:
        text = (array.group(1) or array.group(2)).strip()
    lowered = text.lower()
    if lowered in {"array", "list"}:
        raise ValueError("Name the item type of a list, such as Room[] or string[].")
    if lowered in PRIMITIVE_TYPES or lowered in TYPE_ALIASES:
        return TYPE_ALIASES.get(lowered, lowered), bool(array)
    name = text[:1].upper() + text[1:]
    if not SCHEMA_NAME.fullmatch(name):
        raise ValueError(
            f"Unknown type {value!r}. Use {', '.join(PRIMITIVE_TYPES)} or a schema name."
        )
    return name, bool(array)


NamedItems = list[dict[str, Any]] | dict[str, dict[str, Any]]


def decode_json(value: Any) -> Any:
    """Local models sometimes send a list or object argument as a JSON string."""
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            return value
        if isinstance(decoded, (list, dict)):
            return decoded
    return value


JsonArg = BeforeValidator(decode_json)


def named_list(items: Any) -> list[dict[str, Any]]:
    """Also reads JSON Schema style maps keyed by name: {"id": {"type": "string"}}."""
    if isinstance(items, dict):
        return [{**fields, "name": key} for key, fields in items.items()
                if isinstance(fields, dict)]
    return list(items or [])


def property_type(item: dict[str, Any]) -> tuple[str, bool]:
    """Also reads JSON Schema style: {"type": "array", "items": {"$ref": "..."}}."""
    items = item.get("items")
    if str(item.get("type", "")).strip().lower() == "array" and isinstance(items, dict):
        return property_type(items)[0], True
    ref = item.get("$ref")
    if isinstance(ref, str) and ref.strip():
        return parse_type(ref.rsplit("/", 1)[-1])
    return parse_type(str(item.get("type") or "string"))


def normalize_path(value: str) -> str:
    path = "/" + value.strip().strip("/")
    # Accept Express-style parameters such as /rooms/:room_id.
    path = re.sub(r"/:([A-Za-z_][A-Za-z0-9_]*)", r"/{\1}", path)
    if not PATH_PATTERN.fullmatch(path):
        raise ValueError(
            "path must look like /rooms/{room_id}: letters, digits, '-', '_' and "
            "{parameters} separated by '/'."
        )
    return path


def _text(value: str | None) -> str:
    return (value or "").strip()


@dataclass
class ApiDesignContext:
    """In-memory API model for one turn; the workflow stores the result."""

    info: ApiInfo
    schemas: dict[str, Schema] = field(default_factory=dict)
    endpoints: dict[str, Endpoint] = field(default_factory=dict)
    requirement_ids: set[str] = field(default_factory=set)
    language: str | None = None
    # An existing service stays in the language it is written in.
    language_fixed: bool = False

    @classmethod
    def from_model(cls, api: ApiModel, requirement_ids: set[str] | None = None,
                   language: str | None = None, language_fixed: bool = False,
                   ) -> "ApiDesignContext":
        return cls(
            dict(api["info"]),  # type: ignore[arg-type]
            {item["name"]: item for item in api["schemas"]},
            {endpoint_key(item): item for item in api["endpoints"]},
            requirement_ids or set(),
            language,
            language_fixed,
        )

    def as_model(self) -> ApiModel:
        def order(item: Endpoint) -> tuple[str, int]:
            return item["path"], HTTP_METHODS.index(item["method"])
        return {
            "info": dict(self.info),  # type: ignore[typeddict-item]
            "schemas": list(self.schemas.values()),
            "endpoints": sorted(self.endpoints.values(), key=order),
        }

    def set_language(self, language: str) -> str:
        """Only registered languages can be recorded; anything else raises."""
        if self.language_fixed:
            raise ValueError(f"The existing service is written in {self.language}, and a "
                             "change to it cannot choose another language.")
        self.language = get_language(language).name
        return self.language

    def set_info(self, title: str = "", version: str = "", description: str = "",
                 auth: str = "") -> ApiInfo:
        if _text(title):
            self.info["title"] = _text(title)
        if _text(version):
            self.info["version"] = _text(version)
        if _text(description):
            self.info["description"] = _text(description)
        if _text(auth):
            scheme = _text(auth).lower().replace("-", "_").replace(" ", "_")
            if scheme not in AUTH_SCHEMES:
                raise ValueError(f"auth must be one of {', '.join(AUTH_SCHEMES)}.")
            self.info["auth"] = scheme  # type: ignore[typeddict-item]
        return self.info

    def record_schema(
        self, name: str, description: str,
        properties: list[PropertyInput] | NamedItems, required: list[str] | None = None,
    ) -> Schema:
        schema_name, array = parse_type(name)
        if array or schema_name in PRIMITIVE_TYPES:
            raise ValueError("name must be a schema name in PascalCase, such as Booking.")
        properties = named_list(properties)
        if not properties:
            raise ValueError("a schema needs at least one property.")
        required_names = set(required or [])
        recorded: list[Property] = []
        for item in properties:
            property_name = _text(item.get("name"))
            if not FIELD_NAME.fullmatch(property_name):
                raise ValueError(f"Invalid property name {property_name!r}.")
            if any(existing["name"] == property_name for existing in recorded):
                raise ValueError(f"Property {property_name} is listed twice.")
            type_name, array = property_type(item)  # type: ignore[arg-type]
            prop: Property = {
                "name": property_name, "type": type_name,
                "required": bool(item.get("required", False)) or property_name in required_names,
                "array": array or bool(item.get("array", False)),
            }
            if _text(item.get("format")):
                prop["format"] = _text(item.get("format"))
            enum = [str(value).strip() for value in item.get("enum") or [] if str(value).strip()]
            if enum:
                prop["enum"] = enum
            if _text(item.get("description")):
                prop["description"] = _text(item.get("description"))
            recorded.append(prop)
        schema: Schema = {
            "name": schema_name, "description": _text(description), "properties": recorded,
        }
        self.schemas[schema_name] = schema
        return schema

    def remove_schema(self, name: str) -> str:
        schema_name, _ = parse_type(name)
        if self.schemas.pop(schema_name, None) is None:
            raise ValueError(f"Unknown schema {schema_name}.")
        return schema_name

    def _parameters(self, parameters: NamedItems, path: str) -> list[Parameter]:
        recorded: list[Parameter] = []
        for item in named_list(parameters):
            name = _text(item.get("name"))
            location = _text(item.get("location")).lower() or (
                "path" if f"{{{name}}}" in path else "query"
            )
            if location not in PARAMETER_LOCATIONS:
                raise ValueError("parameter location must be path, query, or header.")
            pattern = HEADER_NAME if location == "header" else FIELD_NAME
            if not pattern.fullmatch(name):
                raise ValueError(f"Invalid parameter name {name!r}.")
            if location == "path" and f"{{{name}}}" not in path:
                raise ValueError(f"Path parameter {name} is not in the path {path}.")
            if any(existing["name"] == name for existing in recorded):
                raise ValueError(f"Parameter {name} is listed twice.")
            type_name, array = parse_type(item.get("type") or "string")
            if array or type_name not in PRIMITIVE_TYPES or type_name == "object":
                raise ValueError("parameter type must be string, integer, number, or boolean.")
            parameter: Parameter = {
                "name": name, "location": location, "type": type_name,  # type: ignore[typeddict-item]
                "required": location == "path" or bool(item.get("required", False)),
            }
            if _text(item.get("description")):
                parameter["description"] = _text(item.get("description"))
            recorded.append(parameter)
        return recorded

    def record_endpoint(
        self, method: str, path: str, summary: str, description: str = "", tag: str = "",
        parameters: NamedItems | None = None, request_schema: str | None = None,
        response_status: int | None = None, response_schema: str | None = None,
        response_array: bool = False, error_statuses: list[int] | None = None,
        requirement_ids: list[str] | None = None,
    ) -> Endpoint:
        method = _text(method).upper()
        if method not in HTTP_METHODS:
            raise ValueError(f"method must be one of {', '.join(HTTP_METHODS)}.")
        path = normalize_path(path)
        if not _text(summary):
            raise ValueError("summary cannot be empty.")
        request = None
        if _text(request_schema):
            request, array = parse_type(request_schema or "")
            if array:
                raise ValueError("request_schema must be a single schema, not a list.")
        response = None
        if _text(response_schema):
            response, array = parse_type(response_schema or "")
            response_array = response_array or array
        status = response_status or DEFAULT_STATUSES.get(method, 200)
        if not 100 <= status <= 599:
            raise ValueError("response_status must be an HTTP status code.")
        errors = sorted(set(error_statuses or []))
        if any(not 400 <= code <= 599 for code in errors):
            raise ValueError("error_statuses must be 4xx or 5xx codes.")
        linked = [item.strip().upper() for item in requirement_ids or [] if item.strip()]
        unknown = [item for item in linked if self.requirement_ids
                   and item not in self.requirement_ids]
        if unknown:
            raise ValueError(f"Unknown requirement ids: {', '.join(unknown)}.")
        endpoint: Endpoint = {
            "method": method, "path": path, "summary": _text(summary),  # type: ignore[typeddict-item]
            "description": _text(description), "tag": _text(tag),
            "parameters": self._parameters(parameters or [], path),
            "request_schema": request, "response_status": status,
            "response_schema": response, "response_array": bool(response_array),
            "error_statuses": errors, "requirement_ids": list(dict.fromkeys(linked)),
        }
        self.endpoints[endpoint_key(endpoint)] = endpoint
        return endpoint

    def remove_endpoint(self, method: str, path: str) -> str:
        key = f"{_text(method).upper()} {normalize_path(path)}"
        if self.endpoints.pop(key, None) is None:
            raise ValueError(f"Unknown endpoint {key}.")
        return key


def build(ctx: ApiDesignContext) -> list[BaseTool]:
    def result(action: str, **values: Any) -> str:
        problems = design_problems(ctx.as_model(), ctx.language)
        return json.dumps({"action": action, **values, "problems": problems},
                          ensure_ascii=False)

    @tool
    async def set_api_info(
        title: str = "", version: str = "", description: str = "", auth: str = ""
    ) -> str:
        """Set the API title, version, description, or authentication.

        Only the arguments you pass change. auth: none, bearer (tokens such as
        JWT or OAuth2 access tokens), api_key (X-API-Key header), or basic.
        """
        try:
            info = ctx.set_info(title, version, description, auth)
        except ValueError as exc:
            logger.warning("API info not recorded: %s", exc)
            return f"API info not recorded: {exc}"
        return result("recorded", info=info)

    @tool
    async def set_language(language: str) -> str:
        """Record the language the server code is generated in.

        Only a supported language is accepted; the error lists them.
        """
        try:
            recorded = ctx.set_language(language)
        except ValueError as exc:
            logger.warning("Language not recorded: %s", exc)
            return f"Language not recorded: {exc}"
        return result("recorded", language=recorded)

    @tool
    async def record_schema(
        name: str, properties: Annotated[list[PropertyInput] | NamedItems, JsonArg],
        description: str = "", required: Annotated[list[str] | None, JsonArg] = None,
    ) -> str:
        """Add a JSON data schema, or replace the schema with the same name.

        name: PascalCase, such as Booking or BookingCreate.
        properties: every field of the schema. type is string, integer, number,
            boolean, object, or another schema's name; set array to true for a
            list. format is optional (date, date-time, email, uuid, uri...).
        """
        try:
            schema = ctx.record_schema(name, description, properties, required)
        except ValueError as exc:
            logger.warning("Schema not recorded: %s", exc)
            return f"Schema not recorded: {exc}"
        return result("recorded", schema=schema)

    @tool
    async def remove_schema(name: str) -> str:
        """Remove a schema the user rejected or that is no longer needed."""
        try:
            removed = ctx.remove_schema(name)
        except ValueError as exc:
            logger.warning("Schema not removed: %s", exc)
            return f"Schema not removed: {exc}"
        return result("removed", name=removed)

    @tool
    async def record_endpoint(
        method: str, path: str, summary: str, description: str = "", tag: str = "",
        parameters: Annotated[list[ParameterInput] | NamedItems | None, JsonArg] = None,
        query_parameters: Annotated[list[ParameterInput] | NamedItems | None, JsonArg] = None,
        request_schema: str | None = None, response_status: int | None = None,
        response_schema: str | None = None, response_array: bool = False,
        error_statuses: Annotated[list[int] | None, JsonArg] = None,
        requirement_ids: Annotated[list[str] | None, JsonArg] = None,
    ) -> str:
        """Add an endpoint, or replace the endpoint with the same method and path.

        method: GET, POST, PUT, PATCH, or DELETE.
        path: such as /rooms/{room_id}; path parameters are strings unless declared.
        parameters: query or header parameters (location defaults to query), and
            path parameters that are not strings.
        request_schema / response_schema: a schema name; omit when there is no body.
        response_status defaults to 201 for POST, 204 for DELETE, otherwise 200.
        response_array: true when the response is a list of response_schema.
        error_statuses: expected error codes, such as [400, 404].
        requirement_ids: the requirements this endpoint serves, such as ["FR-1"].
        """
        try:
            endpoint = ctx.record_endpoint(
                method, path, summary, description, tag,
                [*named_list(parameters),
                 *({**item, "location": "query"} for item in named_list(query_parameters))],
                request_schema,
                response_status, response_schema, response_array, error_statuses,
                requirement_ids,
            )
        except ValueError as exc:
            logger.warning("Endpoint not recorded: %s", exc)
            return f"Endpoint not recorded: {exc}"
        return result("recorded", endpoint=endpoint)

    @tool
    async def remove_endpoint(method: str, path: str) -> str:
        """Remove an endpoint the user rejected or that was recorded by mistake."""
        try:
            removed = ctx.remove_endpoint(method, path)
        except ValueError as exc:
            logger.warning("Endpoint not removed: %s", exc)
            return f"Endpoint not removed: {exc}"
        return result("removed", endpoint=removed)

    @tool
    async def show_api() -> str:
        """Show the whole API model recorded so far and its open problems."""
        return json.dumps({**ctx.as_model(), "language": ctx.language,
                           "problems": design_problems(ctx.as_model(), ctx.language)},
                          ensure_ascii=False)

    return [set_api_info, set_language, record_schema, remove_schema, record_endpoint,
            remove_endpoint, show_api]
