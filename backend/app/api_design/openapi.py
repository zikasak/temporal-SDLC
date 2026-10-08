"""Checks the API model and renders it as an OpenAPI 3.1 document."""

import re
from http import HTTPStatus
from typing import Any

import yaml

from app.api_design.models import PRIMITIVE_TYPES, ApiModel, Endpoint, Property

DOCUMENT_FILENAME = "openapi.yaml"
PATH_PATTERN = re.compile(r"/|(/([A-Za-z0-9._~-]+|\{[A-Za-z_][A-Za-z0-9_]*\}))+")
PATH_PARAMETER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
SECURITY_SCHEMES: dict[str, dict[str, str]] = {
    "bearer": {"type": "http", "scheme": "bearer"},
    "basic": {"type": "http", "scheme": "basic"},
    "api_key": {"type": "apiKey", "in": "header", "name": "X-API-Key"},
}
ERROR_SCHEMA = {
    "type": "object",
    "properties": {"detail": {"type": "string"}},
    "required": ["detail"],
}


class _Dumper(yaml.SafeDumper):
    # Shared dicts must not turn into YAML anchors and aliases.
    def ignore_aliases(self, data: Any) -> bool:
        return True


def endpoint_key(endpoint: Endpoint) -> str:
    return f"{endpoint['method']} {endpoint['path']}"


LANGUAGE_PROBLEM = "The implementation language is not chosen."


def design_problems(api: ApiModel, language: str | None) -> list[str]:
    """What must be settled before the OpenAPI document and its code can be generated."""
    return [*api_problems(api), *([] if language else [LANGUAGE_PROBLEM])]


def api_problems(api: ApiModel) -> list[str]:
    """What must be fixed before the model can become a valid OpenAPI document."""
    problems: list[str] = []
    schema_names = {schema["name"] for schema in api["schemas"]}

    def check_type(type_name: str, where: str) -> None:
        if type_name not in PRIMITIVE_TYPES and type_name not in schema_names:
            problems.append(f"{where} uses the unknown schema {type_name}.")

    for schema in api["schemas"]:
        for item in schema["properties"]:
            check_type(item["type"], f"Property {schema['name']}.{item['name']}")
    if not api["endpoints"]:
        problems.append("No endpoints have been recorded yet.")
    for endpoint in api["endpoints"]:
        key = endpoint_key(endpoint)
        if not PATH_PATTERN.fullmatch(endpoint["path"]):
            problems.append(f"{key} has a malformed path.")
        path_names = set(PATH_PARAMETER.findall(endpoint["path"]))
        for parameter in endpoint["parameters"]:
            if parameter["location"] == "path" and parameter["name"] not in path_names:
                problems.append(
                    f"{key} declares the path parameter {parameter['name']}, "
                    "which is not in its path."
                )
        for schema_name in (endpoint["request_schema"], endpoint["response_schema"]):
            if schema_name:
                check_type(schema_name, key)
    return problems


def _ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{name}"}


def _type_schema(type_name: str) -> dict[str, Any]:
    return {"type": type_name} if type_name in PRIMITIVE_TYPES else _ref(type_name)


def _property_schema(item: Property) -> dict[str, Any]:
    schema = _type_schema(item["type"])
    if item.get("format"):
        schema["format"] = item["format"]
    if item.get("enum"):
        schema["enum"] = item["enum"]
    if item["array"]:
        schema = {"type": "array", "items": schema}
    if item.get("description"):
        schema["description"] = item["description"]
    return schema


def _operation_id(endpoint: Endpoint, used: set[str]) -> str:
    words = [endpoint["method"].lower()]
    for segment in endpoint["path"].strip("/").split("/"):
        match = PATH_PARAMETER.fullmatch(segment)
        parts = re.split(r"[^A-Za-z0-9]+", match.group(1) if match else segment)
        if match:
            words.append("By")
        words += [part[:1].upper() + part[1:] for part in parts if part]
    base = "".join(words)
    operation_id, number = base, 2
    while operation_id in used:
        operation_id, number = f"{base}{number}", number + 1
    used.add(operation_id)
    return operation_id


def _reason(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Response"


def _operation(endpoint: Endpoint, operation_id: str) -> dict[str, Any]:
    operation: dict[str, Any] = {"operationId": operation_id, "summary": endpoint["summary"]}
    if endpoint["description"]:
        operation["description"] = endpoint["description"]
    if endpoint["tag"]:
        operation["tags"] = [endpoint["tag"]]
    declared = {item["name"]: item for item in endpoint["parameters"]}
    parameters = []
    for name in PATH_PARAMETER.findall(endpoint["path"]):
        item = declared.get(name)
        parameter: dict[str, Any] = {
            "name": name, "in": "path", "required": True,
            "schema": {"type": item["type"] if item else "string"},
        }
        if item and item.get("description"):
            parameter["description"] = item["description"]
        parameters.append(parameter)
    for item in endpoint["parameters"]:
        if item["location"] == "path":
            continue
        parameter = {"name": item["name"], "in": item["location"],
                     "required": item["required"], "schema": {"type": item["type"]}}
        if item.get("description"):
            parameter["description"] = item["description"]
        parameters.append(parameter)
    if parameters:
        operation["parameters"] = parameters
    if endpoint["request_schema"]:
        operation["requestBody"] = {
            "required": True,
            "content": {"application/json": {"schema": _type_schema(endpoint["request_schema"])}},
        }
    status = endpoint["response_status"]
    response: dict[str, Any] = {"description": _reason(status)}
    if endpoint["response_schema"]:
        schema = _type_schema(endpoint["response_schema"])
        if endpoint["response_array"]:
            schema = {"type": "array", "items": schema}
        response["content"] = {"application/json": {"schema": schema}}
    responses: dict[str, Any] = {str(status): response}
    for error in endpoint["error_statuses"]:
        responses.setdefault(str(error), {
            "description": _reason(error),
            "content": {"application/json": {"schema": _ref("Error")}},
        })
    operation["responses"] = responses
    if endpoint["requirement_ids"]:
        operation["x-requirements"] = endpoint["requirement_ids"]
    return operation


def render_openapi(api: ApiModel) -> str:
    info = api["info"]
    document: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {"title": info["title"], "version": info["version"]},
    }
    if info["description"]:
        document["info"]["description"] = info["description"]
    schemas: dict[str, Any] = {}
    for schema in api["schemas"]:
        rendered: dict[str, Any] = {"type": "object"}
        if schema["description"]:
            rendered["description"] = schema["description"]
        rendered["properties"] = {
            item["name"]: _property_schema(item) for item in schema["properties"]
        }
        required = [item["name"] for item in schema["properties"] if item["required"]]
        if required:
            rendered["required"] = required
        schemas[schema["name"]] = rendered
    if any(endpoint["error_statuses"] for endpoint in api["endpoints"]):
        schemas.setdefault("Error", ERROR_SCHEMA)
    paths: dict[str, Any] = {}
    used: set[str] = set()
    for endpoint in api["endpoints"]:
        paths.setdefault(endpoint["path"], {})[endpoint["method"].lower()] = _operation(
            endpoint, _operation_id(endpoint, used)
        )
    document["paths"] = paths
    components: dict[str, Any] = {}
    if schemas:
        components["schemas"] = schemas
    if info["auth"] in SECURITY_SCHEMES:
        components["securitySchemes"] = {info["auth"]: SECURITY_SCHEMES[info["auth"]]}
        document["security"] = [{info["auth"]: []}]
    if components:
        document["components"] = components
    return yaml.dump(document, Dumper=_Dumper, sort_keys=False, allow_unicode=True)
