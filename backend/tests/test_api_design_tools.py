import json

import pytest

from app.api_design.models import empty_api
from app.tools.api_design import ApiDesignContext, build, parse_type


def context(language: str | None = "java") -> ApiDesignContext:
    return ApiDesignContext.from_model(empty_api(), {"FR-1", "FR-2"}, language)


@pytest.mark.parametrize(("value", "expected"), [
    ("string", ("string", False)),
    ("Int", ("integer", False)),
    ("booking", ("Booking", False)),
    ("Room[]", ("Room", True)),
    ("list[str]", ("string", True)),
    ("array<Room>", ("Room", True)),
])
def test_parse_type(value, expected):
    assert parse_type(value) == expected


@pytest.mark.parametrize("value", ["array", "room type", "1Room"])
def test_parse_type_rejects(value):
    with pytest.raises(ValueError):
        parse_type(value)


def test_record_endpoint_normalizes_and_updates_by_key():
    ctx = context()
    first = ctx.record_endpoint("post", "rooms/", "Create a room", request_schema="RoomCreate",
                                response_schema="Room", requirement_ids=["fr-1"])
    assert first["path"] == "/rooms" and first["method"] == "POST"
    assert first["response_status"] == 201 and first["requirement_ids"] == ["FR-1"]
    ctx.record_endpoint("GET", "/rooms/:room_id", "Get a room", response_schema="Room",
                        parameters=[{"name": "room_id", "type": "int"}], error_statuses=[404])
    updated = ctx.record_endpoint("POST", "/rooms", "Create one room")
    assert updated["summary"] == "Create one room"
    model = ctx.as_model()
    assert [(item["method"], item["path"]) for item in model["endpoints"]] == [
        ("POST", "/rooms"), ("GET", "/rooms/{room_id}"),
    ]
    parameter = model["endpoints"][1]["parameters"][0]
    assert parameter["location"] == "path" and parameter["type"] == "integer"
    assert parameter["required"] is True


@pytest.mark.parametrize("kwargs", [
    {"method": "FETCH"},
    {"path": "/rooms/{room id}"},
    {"summary": " "},
    {"requirement_ids": ["FR-9"]},
    {"error_statuses": [200]},
    {"request_schema": "Room[]"},
    {"parameters": [{"name": "room_id", "location": "path"}]},
    {"parameters": [{"name": "filter", "type": "Room"}]},
])
def test_record_endpoint_rejects_invalid_input(kwargs):
    arguments = {"method": "GET", "path": "/rooms", "summary": "List rooms", **kwargs}
    with pytest.raises(ValueError):
        context().record_endpoint(**arguments)


def test_record_and_remove_schema():
    ctx = context()
    schema = ctx.record_schema("room", "A bookable room", [
        {"name": "id", "type": "string", "format": "uuid", "required": True},
        {"name": "tags", "type": "string[]"},
        {"name": "kind", "type": "string", "enum": ["small", " large "]},
    ])
    assert schema["name"] == "Room"
    assert [item["array"] for item in schema["properties"]] == [False, True, False]
    assert schema["properties"][2]["enum"] == ["small", "large"]
    with pytest.raises(ValueError):
        ctx.record_schema("Room", "", [{"name": "id", "type": "string"},
                                       {"name": "id", "type": "integer"}])
    assert ctx.remove_schema("Room") == "Room"
    with pytest.raises(ValueError):
        ctx.remove_schema("Room")


def test_remove_endpoint():
    ctx = context()
    ctx.record_endpoint("DELETE", "/rooms/{room_id}", "Delete a room")
    assert ctx.remove_endpoint("delete", "/rooms/{room_id}/") == "DELETE /rooms/{room_id}"
    with pytest.raises(ValueError):
        ctx.remove_endpoint("DELETE", "/rooms/{room_id}")


def test_set_info():
    ctx = context()
    info = ctx.set_info(title="Rooms API", auth="API-Key")
    assert info == {"title": "Rooms API", "version": "1.0.0", "description": "",
                    "auth": "api_key"}
    with pytest.raises(ValueError):
        ctx.set_info(auth="oauth3")


async def test_tools_report_problems_and_errors():
    ctx = context()
    tools = {item.name: item for item in build(ctx)}
    result = json.loads(await tools["record_endpoint"].ainvoke({
        "method": "GET", "path": "/rooms", "summary": "List rooms",
        "response_schema": "Room", "response_array": True,
        "parameters": [{"name": "page", "type": "integer"}],
    }))
    assert result["action"] == "recorded"
    assert result["problems"] == ["GET /rooms uses the unknown schema Room."]
    result = json.loads(await tools["record_schema"].ainvoke({
        "name": "Room", "description": "", "properties": [{"name": "id", "type": "string"}],
    }))
    assert result["problems"] == []
    failed = await tools["remove_schema"].ainvoke({"name": "Booking"})
    assert failed.startswith("Schema not removed:")
    shown = json.loads(await tools["show_api"].ainvoke({}))
    assert [item["name"] for item in shown["schemas"]] == ["Room"]


async def test_tools_accept_json_schema_style_arguments():
    ctx = context()
    tools = {item.name: item for item in build(ctx)}
    result = json.loads(await tools["record_schema"].ainvoke({
        "name": "RoomSearch",
        "properties": {
            "check_in": {"type": "string", "format": "date"},
            "guests": {"type": "integer"},
            "rooms": {"type": "array", "items": {"$ref": "#/components/schemas/Room"}},
        },
        "required": ["check_in"],
    }))
    properties = result["schema"]["properties"]
    assert [(item["name"], item["type"], item["required"], item["array"])
            for item in properties] == [
        ("check_in", "string", True, False),
        ("guests", "integer", False, False),
        ("rooms", "Room", False, True),
    ]
    result = json.loads(await tools["record_endpoint"].ainvoke({
        "method": "GET", "path": "/rooms", "summary": "Search rooms",
        "query_parameters": {"guests": {"type": "integer"}},
        "parameters": [{"name": "page", "type": "integer"}],
    }))
    assert [(item["name"], item["location"], item["type"])
            for item in result["endpoint"]["parameters"]] == [
        ("page", "query", "integer"), ("guests", "query", "integer"),
    ]


async def test_tools_decode_json_string_arguments():
    ctx = context()
    tools = {item.name: item for item in build(ctx)}
    result = json.loads(await tools["record_schema"].ainvoke({
        "name": "EchoObject",
        "properties": json.dumps([
            {"name": "value", "type": "string"},
            {"name": "type", "type": "string", "required": True},
        ]),
        "required": '["value"]',
    }))
    assert [(item["name"], item["required"]) for item in result["schema"]["properties"]] == [
        ("value", True), ("type", True),
    ]
    result = json.loads(await tools["record_endpoint"].ainvoke({
        "method": "POST", "path": "/echo", "summary": "Echo",
        "request_schema": "EchoObject", "response_schema": "EchoObject",
        "query_parameters": '{"verbose": {"type": "boolean"}}',
        "error_statuses": "[400]", "requirement_ids": '["FR-1"]',
    }))
    assert result["action"] == "recorded"
    assert result["endpoint"]["error_statuses"] == [400]


async def test_set_language_records_only_supported_languages():
    ctx = context(language=None)
    tools = {item.name: item for item in build(ctx)}
    assert "The implementation language is not chosen." in json.loads(
        await tools["show_api"].ainvoke({}))["problems"]
    refused = await tools["set_language"].ainvoke({"language": "cobol"})
    assert refused.startswith("Language not recorded:") and "java" in refused
    assert ctx.language is None
    result = json.loads(await tools["set_language"].ainvoke({"language": " Java "}))
    assert result["language"] == "java" and ctx.language == "java"
    assert "The implementation language is not chosen." not in result["problems"]


def test_tool_schemas_still_describe_lists():
    tools = {item.name: item for item in build(context())}
    schema = tools["record_schema"].tool_call_schema.model_json_schema()
    options = [option.get("type") for option in schema["properties"]["properties"]["anyOf"]]
    assert "array" in options
    assert "string" not in options
