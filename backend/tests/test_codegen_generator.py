import shlex
import shutil
from pathlib import Path

import pytest

from app.api_design.openapi import render_openapi
from app.codegen.generator import CodegenError, generate_spring_server
from app.config import OPENAPI_GENERATOR_CMD
from tests.test_openapi_render import sample_api

command = shlex.split(OPENAPI_GENERATOR_CMD)
pytestmark = pytest.mark.skipif(
    shutil.which(command[0]) is None
    or not all(Path(part).exists() for part in command if part.endswith(".jar")),
    reason="openapi-generator is not available",
)


async def test_generates_spring_interfaces_and_models(tmp_path):
    target = tmp_path / "session" / "server"
    target.mkdir(parents=True)
    (target / "stale.txt").write_text("old")

    files = await generate_spring_server(render_openapi(sample_api()), target)

    sources = target / "src" / "main" / "java" / "com" / "example"
    assert files > 0
    assert (sources / "api" / "RoomsApi.java").is_file()
    assert (sources / "model" / "Room.java").is_file()
    assert "interface RoomsApi" in (sources / "api" / "RoomsApi.java").read_text()
    tests = target / "src" / "test" / "java" / "com" / "example" / "api"
    rooms_test = (tests / "RoomsApiTest.java").read_text()
    assert "class RoomsApiTest" in rooms_test and "void getRooms()" in rooms_test
    default_test = (tests / "DefaultApiTest.java").read_text()
    assert "void getRoomsByRoomId()" in default_test
    assert "void deleteRoomsByRoomId()" in default_test
    assert not (target / "stale.txt").exists()
    assert not list(target.glob(".openapi-generator*"))
    assert [item.name for item in tmp_path.joinpath("session").iterdir()] == ["server"]


async def test_invalid_spec_raises(tmp_path):
    with pytest.raises(CodegenError):
        await generate_spring_server("not: [an openapi document", tmp_path / "server")
    assert not (tmp_path / "server").exists()
