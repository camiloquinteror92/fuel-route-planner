"""Run the project's pytest suite from the page: ``POST /api/tests/run``, ``GET /api/tests``.

* ``inventory()``: every test file and test function, read from the source with the
  AST (no import, no run): the file's title is the first paragraph of its docstring,
  a test's description the first paragraph of its docstring (joined into one line),
  or its name in words when it has none.
* ``run_tests()``: runs ``python -m pytest`` in a subprocess with the SAME
  interpreter, in the project directory, with a JUnit report written to a temporary
  file, and a timeout. The command is fixed: nothing from the request reaches it.
  One run at a time per process (a second one gets ``SuiteRunInProgress``, a 409).
  The report is parsed into groups per file, then copied to ``TEST_REPORT_FILE`` so
  ``/api/about`` (the Requirements tab) shows the same run.
* ``runner_status(request)``: the runner works only while ``TEST_RUNNER_ENABLED`` is on
  (by default only under ``manage.py runserver``, see settings) and only for a request
  from this machine: loopback address, a loopback host name (no DNS rebinding), no
  proxy header. Starting a run also needs proof that the page itself sent it (an Origin
  equal to the host, or ``Sec-Fetch-Site: same-origin``): curl or a page of another
  site cannot start one. A public deployment never starts processes for its visitors,
  and ``GET /api/tests`` hides failure messages and output from anyone but this machine.
"""

from __future__ import annotations

import ast
import ipaddress
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import DisallowedHost
from django.http.request import split_domain_port

from .errors import SuiteRunFailed, SuiteRunInProgress, SuiteRunnerUnavailable

TEST_DIR = Path("fuelroute") / "tests"
UNAVAILABLE_DETAIL = (
    "The test runner is only available when you run the project locally and open the page on that "
    "machine (127.0.0.1 or localhost). Run `python -m pytest` instead."
)
OFF_DETAIL = (
    "The test runner is off on this server: it is on by default only under `manage.py runserver` "
    "(or with ENABLE_TEST_RUNNER=1), and DISABLE_TEST_RUNNER=1 turns it off. Run `python -m pytest` instead."
)
NO_ORIGIN_DETAIL = (
    "Start the run from the page: a request without an Origin header (curl, a script) cannot start one. "
    "In a terminal, run `python -m pytest`."
)
# Any of these means a proxy (or a client pretending to be one) is in between.
_PROXY_HEADERS = (
    "HTTP_X_FORWARDED_FOR", "HTTP_X_REAL_IP", "HTTP_FORWARDED", "HTTP_X_FORWARDED_HOST", "HTTP_X_FORWARDED_PROTO",
    "HTTP_CF_CONNECTING_IP", "HTTP_TRUE_CLIENT_IP", "HTTP_X_CLIENT_IP", "HTTP_X_CLUSTER_CLIENT_IP", "HTTP_VIA",
)
_OUTCOMES = ("passed", "failed", "error", "skipped")
_TAIL_LINES = 30

_run_lock = threading.Lock()
_state_lock = threading.Lock()
_last_run: dict | None = None
_inventory_cache: dict = {}


def _base_dir() -> Path:
    return Path(settings.BASE_DIR)


def command() -> list[str]:
    """The exact command a run executes (also shown in the response)."""
    return [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--color=no"]


def display_command() -> str:
    return "python -m pytest -q -p no:cacheprovider --color=no --junitxml=<temporary file>"


# --- who may run it -------------------------------------------------------------------------


def _is_loopback(address: str) -> bool:
    try:
        ip = ipaddress.ip_address((address or "").split("%", 1)[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return ip.is_loopback


def _host_name(request) -> str | None:
    try:
        host = request.get_host()
    except DisallowedHost:
        return None
    return split_domain_port(host)[0].strip("[]")


def _loopback_host(request) -> bool:
    """The page was opened at this machine's own name: "localhost" or a loopback address.
    A site whose DNS points at 127.0.0.1 (DNS rebinding) has another name, even when
    DJANGO_ALLOWED_HOSTS lets it through."""
    name = _host_name(request)
    if not name:
        return False
    return name == "localhost" or name.endswith(".localhost") or _is_loopback(name)


def is_local(request) -> bool:
    """From this machine, to this machine, with no proxy in between."""
    return (
        _is_loopback(request.META.get("REMOTE_ADDR", ""))
        and not any(request.META.get(header) for header in _PROXY_HEADERS)
        and _loopback_host(request)
    )


def _same_origin(request, required: bool) -> bool:
    """A browser says where a request comes from (Origin, Sec-Fetch-Site): a page of another
    site is refused; with ``required``, so is a request that does not say it (curl, a script)."""
    site = request.META.get("HTTP_SEC_FETCH_SITE", "")
    if site in ("cross-site", "same-site"):
        return False
    origin = request.META.get("HTTP_ORIGIN")
    if origin is not None:
        if origin == "null":
            return False
        try:
            host = request.get_host().lower()
        except DisallowedHost:
            return False
        return urlsplit(origin).netloc.lower() == host
    return site == "same-origin" or not required


def runner_status(request, starting: bool = False) -> dict:
    """{"available": bool, "detail": str}: may THIS request start a run (``starting``: it is
    the POST that would start it, so it must also prove it comes from the page)?"""
    if not getattr(settings, "TEST_RUNNER_ENABLED", False):
        return {"available": False, "detail": OFF_DETAIL}
    if not is_local(request):
        return {"available": False, "detail": UNAVAILABLE_DETAIL}
    if not _same_origin(request, required=False):
        return {"available": False, "detail": "A page of another site cannot start a test run."}
    if starting and not _same_origin(request, required=True):
        return {"available": False, "detail": NO_ORIGIN_DETAIL}
    return {"available": True, "detail": "Runs the whole pytest suite on this machine, in a separate process."}


def check_allowed(request) -> None:
    status = runner_status(request, starting=True)
    if not status["available"]:
        raise SuiteRunnerUnavailable(status["detail"])


def redacted(result: dict | None) -> dict | None:
    """A run as anyone may see it: counts and outcomes, without failure messages or the
    output tail (they can name local paths). ``GET /api/tests`` from another machine."""
    if result is None:
        return None
    result = {**result, "output_tail": []}
    result["groups"] = [
        {**group, "tests": [{**test, "message": None} for test in group["tests"]]} for group in result["groups"]
    ]
    return result


def is_running() -> bool:
    return _run_lock.locked()


# --- inventory --------------------------------------------------------------------------------


def _first_line(text: str | None) -> str:
    text = (text or "").strip()
    return text.splitlines()[0].strip() if text else ""


def _first_paragraph(text: str | None) -> str:
    """The docstring up to its first blank line, as one line (a sentence is never cut)."""
    lines = []
    for line in (text or "").strip().splitlines():
        if not line.strip():
            break
        lines.append(line.strip())
    return " ".join(lines)


# Words that keep their capitals when a test name is turned into a sentence.
_PROPER = {
    "canada": "Canada", "mexico": "Mexico", "alaska": "Alaska", "hawaii": "Hawaii", "usa": "USA", "us": "US",
    "toronto": "Toronto", "osrm": "OSRM", "nominatim": "Nominatim", "json": "JSON", "geojson": "GeoJSON",
    "csv": "CSV", "api": "API", "dp": "DP", "html": "HTML", "url": "URL", "ip": "IP",
}


def _words(name: str) -> str:
    """test_route_through_canada -> "Route through Canada"."""
    words = re.sub(r"^test_?", "", name).replace("_", " ").split()
    text = " ".join(_PROPER.get(word.lower(), word) for word in words)
    return text[:1].upper() + text[1:]


def _test_functions(tree: ast.Module) -> list[tuple[str, str]]:
    """(qualified name, description) of the test functions of a module, in source order."""
    found = []
    functions = (ast.FunctionDef, ast.AsyncFunctionDef)
    for node in tree.body:
        if isinstance(node, functions) and node.name.startswith("test"):
            found.append((node.name, _first_paragraph(ast.get_docstring(node)) or _words(node.name)))
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for item in node.body:
                if isinstance(item, functions) and item.name.startswith("test"):
                    description = _first_paragraph(ast.get_docstring(item)) or _words(item.name)
                    found.append((f"{node.name}::{item.name}", description))
    return found


def _parse_file(path: Path) -> dict:
    """{"file", "title", "tests": [{"name", "description"}]} of one test file (cached per mtime)."""
    relative = path.relative_to(_base_dir()).as_posix()
    mtime = path.stat().st_mtime
    with _state_lock:
        cached = _inventory_cache.get(relative)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, ValueError, OSError):
        tree = ast.Module(body=[], type_ignores=[])
    group = {
        "file": relative,
        "title": _first_paragraph(ast.get_docstring(tree)) or _words(path.stem),
        "tests": [{"name": name, "description": text} for name, text in _test_functions(tree)],
    }
    with _state_lock:
        _inventory_cache[relative] = (mtime, group)
    return group


def inventory() -> dict:
    """Every test file and function of the suite (static: nothing is imported or run)."""
    groups = [_parse_file(path) for path in sorted((_base_dir() / TEST_DIR).glob("test_*.py"))]
    return {
        "files": len(groups),
        "tests": sum(len(group["tests"]) for group in groups),
        "groups": groups,
    }


def _descriptions() -> dict[str, dict[str, str]]:
    """file -> {function: description}, and file -> title under the key ""."""
    out = {}
    for group in inventory()["groups"]:
        table = {test["name"]: test["description"] for test in group["tests"]}
        table[""] = group["title"]
        out[group["file"]] = table
    return out


# --- results ------------------------------------------------------------------------------------


def _file_and_name(classname: str, name: str) -> tuple[str, str]:
    """JUnit (classname, name) -> ("fuelroute/tests/test_x.py", "test_y[param]" or "TestC::test_y")."""
    parts = classname.split(".")
    path = "/".join(parts) + ".py"
    if not (_base_dir() / path).exists() and len(parts) > 1:  # a method of a test class
        return "/".join(parts[:-1]) + ".py", f"{parts[-1]}::{name}"
    return path, name


def _outcome(case: ET.Element) -> tuple[str, str | None]:
    for tag, outcome in (("failure", "failed"), ("error", "error"), ("skipped", "skipped")):
        found = case.find(tag)
        if found is not None:
            message = _first_line(found.get("message") or found.text or "")
            return outcome, message[:300] or None
    return "passed", None


def parse_junit(path: Path) -> dict:
    """Totals and groups of a JUnit report, in the shape of ``POST /api/tests/run``."""
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    descriptions = _descriptions()
    groups: dict[str, dict] = {}
    counts = dict.fromkeys(_OUTCOMES, 0)
    duration, ran_at = 0.0, None
    for suite in suites:
        duration += float(suite.get("time", 0) or 0)
        ran_at = ran_at or suite.get("timestamp")
        for case in suite.iter("testcase"):
            file, name = _file_and_name(case.get("classname", ""), case.get("name", ""))
            table = descriptions.get(file, {})
            group = groups.setdefault(
                file, {"file": file, "title": table.get("") or _words(Path(file).stem), "tests": []}
            )
            outcome, message = _outcome(case)
            counts[outcome] += 1
            function = name.split("[", 1)[0]
            group["tests"].append(
                {
                    "name": name,
                    "description": table.get(function) or _words(function.split("::")[-1]),
                    "outcome": outcome,
                    "duration_ms": round(float(case.get("time", 0) or 0) * 1000, 1),
                    "message": message,
                }
            )
    if ran_at:
        try:
            parsed = datetime.fromisoformat(ran_at)
            ran_at = (parsed if parsed.tzinfo else parsed.astimezone()).astimezone(UTC).isoformat()
        except ValueError:
            ran_at = None
    return {
        "passed": counts["passed"],
        "failed": counts["failed"],
        "errors": counts["error"],
        "skipped": counts["skipped"],
        "total": sum(counts.values()),
        "duration_s": round(duration, 2),
        "ran_at": ran_at or datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
        "groups": sorted(groups.values(), key=lambda group: group["file"]),
    }


def _publish_report(source: Path) -> None:
    """Copy the run's JUnit report to TEST_REPORT_FILE (atomically), for /api/about."""
    target = Path(settings.FUEL_PLANNER["TEST_REPORT_FILE"])
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.with_name(target.name + ".tmp")
        shutil.copyfile(source, staging)
        os.replace(staging, target)
    except OSError:  # a read-only checkout: the run is still answered
        pass


def run_tests(timeout_seconds: float | None = None) -> dict:
    """Run the suite once; raises ``SuiteRunInProgress`` while another run is going."""
    global _last_run
    if not _run_lock.acquire(blocking=False):
        raise SuiteRunInProgress("A test run is already in progress; wait for it to finish.")
    try:
        timeout = timeout_seconds or float(getattr(settings, "TEST_RUNNER_TIMEOUT_SECONDS", 180))
        with tempfile.TemporaryDirectory(prefix="fuelroute-tests-") as folder:
            report = Path(folder) / "junit.xml"
            started = time.perf_counter()
            try:
                completed = subprocess.run(
                    [*command(), f"--junitxml={report}"],
                    cwd=_base_dir(),
                    capture_output=True,
                    stdin=subprocess.DEVNULL,  # never the server's console
                    timeout=timeout,
                    env={**os.environ, "PYTHONIOENCODING": "utf-8", "DISABLE_TEST_RUNNER": "1"},
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise SuiteRunFailed(f"The test run took longer than {timeout:.0f} s and was stopped.") from exc
            except OSError as exc:
                raise SuiteRunFailed(f"pytest could not be started: {exc}.") from exc
            wall = time.perf_counter() - started
            if not report.exists():
                output = (completed.stdout or b"").decode("utf-8", "replace").strip().splitlines()
                raise SuiteRunFailed(
                    "pytest did not produce a report (exit code "
                    f"{completed.returncode}): {' / '.join(output[-3:]) or 'no output'}"
                )
            result = parse_junit(report)
            _publish_report(report)
        result.update(
            exit_code=completed.returncode,
            wall_s=round(wall, 2),
            command=display_command(),
            source="page",
            output_tail=(completed.stdout or b"").decode("utf-8", "replace").strip().splitlines()[-_TAIL_LINES:]
            if completed.returncode
            else [],
        )
        with _state_lock:
            _last_run = result
        return result
    finally:
        _run_lock.release()


def last_run() -> dict | None:
    """The last run of this process, else the last local report (``pytest`` in a terminal)."""
    with _state_lock:
        if _last_run is not None:
            return _last_run
    path = Path(settings.FUEL_PLANNER["TEST_REPORT_FILE"])
    try:
        result = parse_junit(path)
    except (OSError, ET.ParseError):
        return None
    result.update(exit_code=None, wall_s=None, command="python -m pytest", source="report_file", output_tail=[])
    return result


def reset() -> None:
    """Forget the last run and the inventory cache (tests)."""
    global _last_run
    with _state_lock:
        _last_run = None
        _inventory_cache.clear()
