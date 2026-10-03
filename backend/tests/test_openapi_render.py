import yaml
from openapi_spec_validator import validate

from app.api_design.models import empty_api
from app.api_design.openapi import api_problems, render_openapi
from app.tools.api_design import ApiDesignContext


def sample_api():
    ctx = ApiDesignContext.from_model(empty_api())
    ctx.set_info(title="Rooms API", description="Book meeting rooms.", auth="bearer")
    ctx.record_schema("Room", "A bookable room", [
        {"name": "id", "type": "string", "format": "uuid", "required": True},
        {"name": "name", "type": "string", "required": True},
        {"name": "bookings", "type": "Booking[]"},
    ])
    ctx.record_schema("Booking", "", [
        {"name": "id", "type": "string", "required": True},
        {"name": "status", "type": "string", "enum": ["held", "confirmed"]},
    ])
    ctx.record_endpoint("GET", "/rooms", "List rooms", tag="Rooms", response_schema="Room",
                        response_array=True, requirement_ids=["FR-1"],
                        parameters=[{"name": "page", "type": "integer"},
                                    {"name": "X-Request-Id", "location": "header"}])
    ctx.record_endpoint("GET", "/rooms/{room_id}", "Get a room", response_schema="Room",
                        error_statuses=[404])
    ctx.record_endpoint("DELETE", "/rooms/{room_id}", "Delete a room",
                        parameters=[{"name": "room_id", "type": "integer"}])
    return ctx.as_model()


def test_render_is_valid_openapi():
    api = sample_api()
    assert api_problems(api) == []
    document = yaml.safe_load(render_openapi(api))
    validate(document)
    assert document["openapi"] == "3.1.0"
    assert document["security"] == [{"bearer": []}]
    room = document["components"]["schemas"]["Room"]
    assert room["required"] == ["id", "name"]
    assert room["properties"]["bookings"] == {
        "type": "array", "items": {"$ref": "#/components/schemas/Booking"}
    }
    assert "Error" in document["components"]["schemas"]

    listing = document["paths"]["/rooms"]["get"]
    assert listing["operationId"] == "getRooms"
    assert listing["x-requirements"] == ["FR-1"]
    assert [item["in"] for item in listing["parameters"]] == ["query", "header"]
    assert listing["responses"]["200"]["content"]["application/json"]["schema"]["type"] == "array"

    item = document["paths"]["/rooms/{room_id}"]
    assert item["get"]["operationId"] == "getRoomsByRoomId"
    assert item["get"]["parameters"][0]["schema"] == {"type": "string"}
    assert item["delete"]["parameters"][0]["schema"] == {"type": "integer"}
    assert item["delete"]["responses"] == {"204": {"description": "No Content"}}
    assert "404" in item["get"]["responses"]


def test_problems():
    assert api_problems(empty_api()) == ["No endpoints have been recorded yet."]
    ctx = ApiDesignContext.from_model(sample_api())
    ctx.remove_schema("Booking")
    ctx.record_endpoint("POST", "/rooms", "Create a room", request_schema="RoomCreate")
    assert api_problems(ctx.as_model()) == [
        "Property Room.bookings uses the unknown schema Booking.",
        "POST /rooms uses the unknown schema RoomCreate.",
    ]
