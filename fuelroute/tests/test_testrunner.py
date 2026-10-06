"""The page's test runner: GET /api/tests (inventory + last run), POST /api/tests/run.

Local requests only (403 otherwise), one run at a time (409), a fixed command that
reads nothing from the request, and results grouped by file with what each test checks.
Most tests fake ``subprocess.run``; one runs a real toy suite end to end.
"""

import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest
from django.conf import settings as project_settings
from rest_framework.test import APIClient

from fuelroute.services import testrunner

REPO = Path(project_settings.BASE_DIR)
JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="1" skipped="1" tests="4" time="2.5"
 timestamp="2026-10-06T10:00:00.000000">
<testcase classname="fuelroute.tests.test_api" name="test_second_request_uses_the_plan_cache" time="0.012" />
<testcase classname="fuelroute.tests.test_api" name="test_invalid_input_returns_400[params0-start]" time="0.003" />
<testcase classname="fuelroute.tests.test_what_if" name="test_out_of_range_settings_are_400[params0-mpg-Must]" time="0.004">
<failure message="AssertionError: assert 200 == 400&#10;second line">trace</failure></testcase>
<testcase classname="fuelroute.tests.test_places" name="test_short_query_returns_nothing[c]" time="0.001">
<skipped message="not today" /></testcase>
</testsuite></testsuites>
"""


@pytest.fixture(autouse=True)
def runner_state(settings, tmp_path):
    testrunner.reset()
    settings.TEST_RUNNER_ENABLED = True  # the page's own run sets DISABLE_TEST_RUNNER for its child
    settings.FUEL_PLANNER = {**settings.FUEL_PLANNER, "TEST_REPORT_FILE": tmp_path / "report" / "pytest.xml"}
    yield
    testrunner.reset()


class FakeRun:
    """Stands in for subprocess.run: records the call and writes a JUnit report."""

    def __init__(self, xml=JUNIT, returncode=1, gate=None, error=None):
        self.calls, self.xml, self.returncode, self.gate, self.error = [], xml, returncode, gate, error

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        if self.gate is not None:
            self.gate.wait(10)
        if self.error is not None:
            raise self.error
        report = next(arg.split("=", 1)[1] for arg in args if arg.startswith("--junitxml="))
        Path(report).write_text(self.xml, encoding="utf-8")
        return subprocess.CompletedProcess(args, self.returncode, stdout=b"1 failed, 2 passed, 1 skipped\n", stderr=b"")


@pytest.fixture
def fake_run(monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(testrunner.subprocess, "run", fake)
    return fake


def _never_run(*args, **kwargs):
    raise AssertionError("the runner must not start pytest for this request")


# --- who may run it ----------------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    "extra",
    [
        {"REMOTE_ADDR": "203.0.113.7"},  # another machine
        {"REMOTE_ADDR": "192.168.1.20"},  # the local network is not this machine either
        {"REMOTE_ADDR": "127.0.0.1", "HTTP_X_FORWARDED_FOR": "203.0.113.7"},  # behind a proxy on this host
        {"REMOTE_ADDR": "127.0.0.1", "HTTP_FORWARDED": "for=203.0.113.7"},
    ],
)
def test_runner_answers_403_unless_the_request_is_local(monkeypatch, extra):
    monkeypatch.setattr(testrunner.subprocess, "run", _never_run)
    response = APIClient().post("/api/tests/run", **extra)
    assert response.status_code == 403
    body = response.json()
    assert body["error"] == "test_runner_unavailable"
    assert "only available when you run the project locally" in body["detail"]
    assert body["meta"] == {"external_api_calls": 0, "external_api_services": []}
    listing = APIClient().get("/api/tests", **extra).json()  # the inventory is still shown
    assert listing["runner"]["available"] is False and listing["inventory"]["tests"] > 0


@pytest.mark.django_db
@pytest.mark.parametrize(
    "extra",
    [{"HTTP_ORIGIN": "https://evil.example"}, {"HTTP_SEC_FETCH_SITE": "cross-site"}, {"HTTP_ORIGIN": "null"}],
)
def test_a_page_of_another_site_cannot_start_a_run(monkeypatch, extra):
    monkeypatch.setattr(testrunner.subprocess, "run", _never_run)
    response = APIClient().post("/api/tests/run", **extra)
    assert response.status_code == 403 and response.json()["error"] == "test_runner_unavailable"


@pytest.mark.django_db
def test_runner_can_be_turned_off(monkeypatch, settings):
    monkeypatch.setattr(testrunner.subprocess, "run", _never_run)
    settings.TEST_RUNNER_ENABLED = False
    response = APIClient().post("/api/tests/run")
    assert response.status_code == 403 and "DISABLE_TEST_RUNNER" in response.json()["detail"]


@pytest.mark.parametrize(
    ("address", "local"),
    [("127.0.0.1", True), ("127.0.0.2", True), ("::1", True), ("::ffff:127.0.0.1", True), ("10.0.0.1", False),
     ("", False), ("not an ip", False), ("::ffff:10.0.0.1", False)],
)
def test_loopback_addresses(address, local):
    assert testrunner._is_loopback(address) is local


# --- a run --------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_run_answers_every_test_grouped_by_file(fake_run, settings):
    client = APIClient()
    response = client.post(
        "/api/tests/run?-k=evil", {"args": ["-k", "x", "--rootdir=/"], "command": "rm"}, format="json",
        HTTP_ORIGIN="http://testserver", HTTP_SEC_FETCH_SITE="same-origin",
    )
    assert response.status_code == 200, response.json()
    body = response.json()

    # The command is fixed: the same interpreter, pytest, a temporary JUnit file, the project directory.
    (args, kwargs), = fake_run.calls
    assert args[:3] == [sys.executable, "-m", "pytest"]
    assert [arg for arg in args if arg.startswith("--junitxml=")] == args[-1:]
    assert not any("evil" in arg or "rm" == arg or arg == "-k" for arg in args)
    assert kwargs["cwd"] == Path(settings.BASE_DIR) and kwargs["timeout"] == settings.TEST_RUNNER_TIMEOUT_SECONDS
    assert kwargs["env"]["DISABLE_TEST_RUNNER"] == "1"

    assert (body["passed"], body["failed"], body["errors"], body["skipped"], body["total"]) == (2, 1, 0, 1, 4)
    assert body["duration_s"] == 2.5 and body["ran_at"].startswith("2026-10-06")
    assert body["exit_code"] == 1 and body["source"] == "page" and body["output_tail"]
    files = [group["file"] for group in body["groups"]]
    assert files == ["fuelroute/tests/test_api.py", "fuelroute/tests/test_places.py", "fuelroute/tests/test_what_if.py"]
    api = body["groups"][0]
    assert api["title"].startswith("The HTTP API end to end")  # the module docstring
    first, param = api["tests"]
    assert first == {
        "name": "test_second_request_uses_the_plan_cache", "description": "Second request uses the plan cache",
        "outcome": "passed", "duration_ms": 12.0, "message": None,
    }
    assert param["name"] == "test_invalid_input_returns_400[params0-start]"
    assert param["description"] == "Invalid input returns 400"
    failed = body["groups"][2]["tests"][0]
    assert failed["outcome"] == "failed" and failed["message"] == "AssertionError: assert 200 == 400"
    assert body["groups"][1]["tests"][0]["outcome"] == "skipped"

    # /api/about reads the same report, and GET /api/tests returns the run.
    assert Path(settings.FUEL_PLANNER["TEST_REPORT_FILE"]).read_text(encoding="utf-8") == JUNIT
    listing = client.get("/api/tests").json()
    assert listing["last_run"] == body
    assert listing["runner"] == {**listing["runner"], "available": True, "running": False}


@pytest.mark.django_db
def test_second_concurrent_run_is_409(monkeypatch):
    gate = threading.Event()
    fake = FakeRun(gate=gate)
    monkeypatch.setattr(testrunner.subprocess, "run", fake)
    answers = {}
    first = threading.Thread(target=lambda: answers.setdefault("first", APIClient().post("/api/tests/run")))
    first.start()
    for _ in range(200):
        if testrunner.is_running():
            break
        threading.Event().wait(0.01)
    assert testrunner.is_running()
    second = APIClient().post("/api/tests/run")
    assert APIClient().get("/api/tests").json()["runner"]["running"] is True
    gate.set()
    first.join(10)
    assert second.status_code == 409 and second.json()["error"] == "test_run_in_progress"
    assert answers["first"].status_code == 200
    assert len(fake.calls) == 1
    assert APIClient().post("/api/tests/run").status_code == 200  # free again


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("error", "text"),
    [
        (subprocess.TimeoutExpired(cmd="pytest", timeout=180), "took longer than"),
        (OSError("no such interpreter"), "could not be started"),
    ],
)
def test_a_run_that_cannot_finish_is_a_clear_error(monkeypatch, error, text):
    monkeypatch.setattr(testrunner.subprocess, "run", FakeRun(error=error))
    response = APIClient().post("/api/tests/run")
    assert response.status_code == 500
    assert response.json()["error"] == "test_run_failed" and text in response.json()["detail"]
    assert not testrunner.is_running()


@pytest.mark.django_db
def test_a_run_without_a_report_says_what_pytest_printed(monkeypatch):
    class NoReport(FakeRun):
        def __call__(self, args, **kwargs):
            return subprocess.CompletedProcess(args, 4, stdout=b"ERROR: usage error\n", stderr=b"")

    monkeypatch.setattr(testrunner.subprocess, "run", NoReport())
    response = APIClient().post("/api/tests/run")
    assert response.status_code == 500 and "usage error" in response.json()["detail"]


# --- inventory and last run ----------------------------------------------------------------------------


@pytest.mark.django_db
def test_inventory_lists_every_test_with_a_description():
    body = APIClient().get("/api/tests").json()
    inventory = body["inventory"]
    files = {group["file"]: group for group in inventory["groups"]}
    on_disk = sorted(path.relative_to(REPO).as_posix() for path in (REPO / "fuelroute" / "tests").glob("test_*.py"))
    assert sorted(files) == on_disk and inventory["files"] == len(on_disk)
    assert inventory["tests"] == sum(len(group["tests"]) for group in inventory["groups"]) > 100
    for group in inventory["groups"]:
        assert group["title"]
        assert all(test["name"].startswith("test") and test["description"] for test in group["tests"])
    what_if = {test["name"]: test["description"] for test in files["fuelroute/tests/test_what_if.py"]["tests"]}
    # A docstring when the test has one, its name in words otherwise.
    assert what_if["test_default_plans_are_identical_to_the_ones_before_what_if_settings"].startswith(
        "The golden file was written by the code"
    )
    assert what_if["test_out_of_range_settings_are_400"] == "Out of range settings are 400"
    assert body["runner"]["command"].startswith("python -m pytest")


@pytest.mark.django_db
def test_last_run_falls_back_to_the_local_report(settings):
    assert APIClient().get("/api/tests").json()["last_run"] is None  # no report yet
    report = Path(settings.FUEL_PLANNER["TEST_REPORT_FILE"])
    report.parent.mkdir(parents=True)
    report.write_text(JUNIT, encoding="utf-8")
    last = APIClient().get("/api/tests").json()["last_run"]
    assert last["source"] == "report_file" and last["total"] == 4 and last["exit_code"] is None


@pytest.mark.django_db
def test_a_real_toy_suite_runs_end_to_end(settings, tmp_path, monkeypatch):
    """No fake: a real pytest subprocess on a toy suite, its JUnit report parsed."""
    tests = tmp_path / "toy" / "fuelroute" / "tests"
    tests.mkdir(parents=True)
    (tests / "test_toy.py").write_text(
        textwrap.dedent(
            '''
            """A toy suite for the runner."""
            import pytest

            def test_adds():
                """One plus one is two."""
                assert 1 + 1 == 2

            def test_breaks():
                assert 1 + 1 == 3

            @pytest.mark.skip(reason="not today")
            def test_later():
                pass

            @pytest.mark.parametrize("n", [1, 2])
            def test_positive(n):
                assert n > 0
            '''
        ),
        encoding="utf-8",
    )
    settings.BASE_DIR = tmp_path / "toy"
    monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)
    result = testrunner.run_tests(timeout_seconds=120)
    assert (result["passed"], result["failed"], result["skipped"], result["errors"]) == (3, 1, 1, 0)
    (group,) = result["groups"]
    assert group["file"] == "fuelroute/tests/test_toy.py" and group["title"] == "A toy suite for the runner."
    outcomes = {test["name"]: (test["outcome"], test["description"]) for test in group["tests"]}
    assert outcomes["test_adds"] == ("passed", "One plus one is two.")
    assert outcomes["test_breaks"][0] == "failed"
    assert outcomes["test_positive[2]"] == ("passed", "Positive")
    assert result["exit_code"] == 1 and any("failed" in line for line in result["output_tail"])
