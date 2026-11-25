"""Routing: seam modules written in path form are matched to the stubbed dotted modules.

The code under test is `check_seams` (app.routing.components) and `validate_completion`
(app.routing.agent); nothing of the service is faked.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.routing.agent import validate_completion
from app.routing.components import check_seams

STUBBED = "app.docker_agents"
OTHER = "app.settings"


def modules() -> dict[str, set[str]]:
    return {STUBBED: {"run_agent"}, OTHER: {"load"}}


def component_route(module: str, added_elsewhere: dict[str, list[str]] | None = None) -> dict[str, Any]:
    seam: dict[str, Any] = {"module": module, "symbols": ["run_agent"]}
    if added_elsewhere is not None:
        seam["added_elsewhere"] = added_elsewhere
    return {"route": "component", "reason": "internal behaviour", "seam": seam}


class TestCheckSeams:
    @pytest.mark.parametrize("written", ["app/docker_agents", "app/docker_agents.py"])
    def test_path_form_module_is_normalized_in_place(self, written: str) -> None:
        # requirement: FR-197, FR-198, FR-200
        routes = {"FR-1": component_route(written)}
        check_seams(routes, modules())
        assert routes["FR-1"]["seam"]["module"] == STUBBED

    @pytest.mark.parametrize("written", ["app/settings", "app/settings.py"])
    def test_path_form_in_added_elsewhere_is_normalized(self, written: str) -> None:
        # requirement: FR-198, FR-200
        routes = {"FR-1": component_route(STUBBED, {written: ["load"]})}
        check_seams(routes, modules())
        added = routes["FR-1"]["seam"]["added_elsewhere"]
        assert list(added) == [OTHER]
        assert added[OTHER] == ["load"]

    def test_dotted_module_is_left_as_is(self) -> None:
        # requirement: FR-198
        # A dotted route is untouched while a path-form route next to it is normalized.
        routes = {"FR-1": component_route(STUBBED, {OTHER: ["load"]}),
                  "FR-2": component_route("app/docker_agents.py")}
        check_seams(routes, modules())
        assert routes["FR-1"]["seam"]["module"] == STUBBED
        assert list(routes["FR-1"]["seam"]["added_elsewhere"]) == [OTHER]
        assert routes["FR-2"]["seam"]["module"] == STUBBED

    @pytest.mark.parametrize("written", ["app.nothing", "app/nothing", "app/nothing.py"])
    def test_module_without_stub_still_fails_with_todays_message(self, written: str) -> None:
        # requirement: FR-200
        # FR-1 is a path form of a stubbed module and must not stop the check; FR-9 has no stub.
        routes = {"FR-1": component_route("app/docker_agents"),
                  "FR-9": component_route(written)}
        with pytest.raises(RuntimeError) as error:
            check_seams(routes, modules())
        assert str(error.value).startswith(
            "The seam of requirement FR-9 names a module the component contract has no stub of:")
        assert written in str(error.value)
        assert routes["FR-1"]["seam"]["module"] == STUBBED

    @pytest.mark.parametrize("written", ["app.nothing", "app/nothing", "app/nothing.py"])
    def test_added_elsewhere_without_stub_still_fails(self, written: str) -> None:
        # requirement: FR-200
        # The seam's own module is a path form of a stub; only the added_elsewhere module has none.
        routes = {"FR-9": component_route("app/docker_agents", {written: ["x"]})}
        with pytest.raises(RuntimeError) as error:
            check_seams(routes, modules())
        message = str(error.value)
        assert "The seam of requirement FR-9 adds symbols to a module the component contract has no stub of" in message
        assert written in message

    def test_other_routes_are_ignored(self) -> None:
        # requirement: FR-198
        routes = {"FR-1": {"route": "http", "reason": "x"},
                  "FR-2": component_route("app/docker_agents")}
        check_seams(routes, modules())
        assert routes["FR-2"]["seam"]["module"] == STUBBED


def write_workspace(root: Path, seam: dict[str, Any]) -> None:
    stub = root / "components" / "app" / "docker_agents.pyi"
    stub.parent.mkdir(parents=True)
    stub.write_text("def run_agent() -> None: ...\n")
    other = root / "components" / "app" / "settings.pyi"
    other.write_text("def load() -> None: ...\n")
    routes = [{"requirement_id": "FR-5", "route": "component",
               "reason": "internal behaviour", "seam": seam}]
    (root / "routes.json").write_text(json.dumps({"routes": routes}))


class TestValidateCompletion:
    @pytest.mark.parametrize("written", ["app/docker_agents", "app/docker_agents.py"])
    def test_path_form_of_one_stub_is_accepted(self, tmp_path: Path, written: str) -> None:
        # requirement: FR-199
        # A path form of one stub does not raise, while an unmatched module next to it does
        # and only that one is named as written.
        write_workspace(tmp_path, {"module": written, "symbols": ["run_agent"],
                                   "added_elsewhere": {"app/missing": ["x"]}})
        with pytest.raises(RuntimeError) as error:
            validate_completion(tmp_path)
        message = str(error.value)
        assert "app/missing" in message
        assert written not in message
        assert STUBBED in message
        assert OTHER in message

    def test_path_form_in_added_elsewhere_is_accepted(self, tmp_path: Path) -> None:
        # requirement: FR-199
        write_workspace(tmp_path, {"module": STUBBED, "symbols": ["run_agent"],
                                   "added_elsewhere": {"app/settings.py": ["load"],
                                                       "app/missing.py": ["x"]}})
        with pytest.raises(RuntimeError) as error:
            validate_completion(tmp_path)
        message = str(error.value)
        assert "app/missing.py" in message
        assert "app/settings.py" not in message
        assert STUBBED in message
        assert OTHER in message

    @pytest.mark.parametrize("written", ["app.missing", "app/missing", "app/missing.py"])
    def test_unmatched_module_is_sent_back_to_the_agent(self, tmp_path: Path, written: str) -> None:
        # requirement: FR-199
        write_workspace(tmp_path, {"module": written, "symbols": ["run_agent"]})
        with pytest.raises(RuntimeError) as error:
            validate_completion(tmp_path)
        message = str(error.value)
        assert written in message
        assert STUBBED in message
        assert OTHER in message

    @pytest.mark.parametrize("written", ["app.missing", "app/missing"])
    def test_unmatched_added_elsewhere_is_sent_back_to_the_agent(
        self, tmp_path: Path, written: str,
    ) -> None:
        # requirement: FR-199
        write_workspace(tmp_path, {"module": STUBBED, "symbols": ["run_agent"],
                                   "added_elsewhere": {written: ["x"]}})
        with pytest.raises(RuntimeError) as error:
            validate_completion(tmp_path)
        message = str(error.value)
        assert written in message
        assert STUBBED in message
