import httpx
import pytest

from app.delivery.ci_activities import log_excerpt, summarize_checks
from app.github import client
from app.github.errors import GitHubError

COMMIT = "a" * 40
CHECKS_PATH = f"/repos/octocat/private-project/commits/{COMMIT}/check-runs"
LOG_PATH = "/repos/octocat/private-project/actions/jobs/7/logs"
LOG_URL = "https://productionresultssa1.blob.core.windows.net/actions-results/7.txt?sig=x"


def run(name: str, status: str = "completed", conclusion: str | None = "success",
        **extra) -> dict:
    return {"id": len(name), "name": name, "status": status, "conclusion": conclusion,
            "url": None, "app": "github-actions", "summary": None, "text": None, **extra}


@pytest.mark.parametrize("runs, status", [
    ([], "none"),
    ([run("backend"), run("frontend", "in_progress", None)], "pending"),
    ([run("backend"), run("lint", conclusion="skipped")], "passed"),
    ([run("backend", conclusion="failure"), run("frontend")], "failed"),
    ([run("backend", conclusion="timed_out")], "failed"),
    ([run("backend"), run("frontend", conclusion="cancelled")], "inconclusive"),
])
def test_the_commit_ci_is_judged_from_all_of_its_checks(runs, status):
    assert summarize_checks(runs)["status"] == status


def test_failed_checks_are_listed_first():
    checks = summarize_checks([run("a-lint"), run("backend", conclusion="failure")])["checks"]
    assert [check["name"] for check in checks] == ["backend", "a-lint"]


def test_a_log_excerpt_ends_at_the_step_that_failed_without_timestamps_or_colours():
    log = "\n".join([
        "2026-11-11T07:59:55.1005573Z ##[group]Run python -m pytest -q",
        "2026-11-11T07:59:55.4074067Z \x1b[31mFAILED tests/test_rooms.py::test_lists\x1b[0m",
        "2026-11-11T07:59:55.4088239Z 1 failed, 709 passed in 532.74s",
        "2026-11-11T07:59:56.2667090Z ##[error]Process completed with exit code 1.",
        "2026-11-11T07:59:57.0000000Z Post job cleanup.",
    ])
    assert log_excerpt(log) == (
        "##[group]Run python -m pytest -q\nFAILED tests/test_rooms.py::test_lists\n"
        "1 failed, 709 passed in 532.74s\n##[error]Process completed with exit code 1.")
    assert log_excerpt("x" * 100, limit=10) == "x" * 10


async def test_check_runs_of_a_commit_are_read(github_http):
    responses, requests = github_http
    responses[CHECKS_PATH] = httpx.Response(200, json={"total_count": 2, "check_runs": [
        {"id": 7, "name": "Backend tests", "status": "completed", "conclusion": "failure",
         "html_url": "https://github.com/octocat/private-project/actions/runs/1/job/7",
         "app": {"slug": "github-actions"}, "output": {"title": None, "summary": None}},
        {"id": "not an id"},
    ]})
    runs = await client.list_check_runs("token", "octocat/private-project", COMMIT)
    assert runs == [{"id": 7, "name": "Backend tests", "status": "completed",
                     "conclusion": "failure",
                     "url": "https://github.com/octocat/private-project/actions/runs/1/job/7",
                     "app": "github-actions", "summary": None, "text": None}]
    assert requests[0].url.params["filter"] == "latest"
    assert requests[0].headers["authorization"] == "Bearer token"


async def test_check_runs_the_app_may_not_read_are_refused(github_http):
    responses, _ = github_http
    responses[CHECKS_PATH] = httpx.Response(403)
    with pytest.raises(GitHubError) as refused:
        await client.list_check_runs("token", "octocat/private-project", COMMIT)
    assert refused.value.status_code == 403
    with pytest.raises(GitHubError):
        await client.list_check_runs("token", "octocat/private-project", "main")


async def test_the_end_of_a_job_log_is_downloaded_without_the_token(github_http):
    responses, requests = github_http
    responses[LOG_PATH] = httpx.Response(302, headers={"location": LOG_URL})
    responses["/actions-results/7.txt"] = httpx.Response(200, text="start\n" + "x" * 50 + "end")
    tail = await client.job_log_tail("token", "octocat/private-project", 7, 10)
    assert tail == "x" * 7 + "end"
    assert "authorization" not in requests[1].headers


@pytest.mark.parametrize("location", [
    "https://attacker.invalid/log.txt", "http://x.blob.core.windows.net/log.txt", ""])
async def test_a_job_log_redirected_elsewhere_is_not_downloaded(github_http, location):
    responses, requests = github_http
    responses[LOG_PATH] = httpx.Response(302, headers={"location": location})
    with pytest.raises(GitHubError):
        await client.job_log_tail("token", "octocat/private-project", 7, 10)
    assert len(requests) == 1
