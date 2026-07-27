"""Tests for ``GET /dashboard``, the local operational dashboard shell.

Requests are driven through the **production** ASGI object (``mgo.api.app:app``
-- the exact app uvicorn serves; there is no router or application factory)
following the pattern established in ``tests/test_app_routes.py`` and
``tests/test_version_api.py``, so route registration and the response content
type are proven where they actually matter. No test dependency is added:
``httpx`` (and therefore FastAPI's ``TestClient``) is not installed, so the
ASGI interface is driven directly.

The dashboard is a **static shell**: it renders no live value server-side.
Consequently these tests need no Raspberry Pi, no camera, no ``vcgencmd``, no
Git, no network, no browser automation, no running server and no database.

Because all live behaviour is browser JavaScript, the browser contract is
established by deterministic assertions over the served source. No JavaScript
test runtime is introduced for this task.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import pytest

import mgo.api.app as app_module
import mgo.core.health as health_module
from mgo.api.app import app
from mgo.api.dashboard_page import render_dashboard_page

#: Every card heading the dashboard must present. The five system fields the
#: project plan makes mandatory -- hostname, uptime, CPU temperature, memory
#: and disk -- are all here, alongside the additional subsystem cards.
_REQUIRED_HEADINGS = (
    "Overall health",
    "Application identity",
    "Hostname",
    "System uptime",
    "CPU utilisation",
    "CPU temperature",
    "Memory",
    "Disk",
    "Database",
    "Camera",
    "Live preview",
    "Motion detection",
    "Notifications",
)

#: The four supported contracts the browser refreshes from.
_REFRESH_SOURCES = (
    "/health",
    "/version",
    "/motion/status",
    "/notifications/status",
)


def _asgi_request(path: str) -> tuple[int, dict[str, str], str]:
    """Perform one in-process HTTP GET against the production ASGI app.

    Returns ``(status_code, lowercased_headers, decoded_body)``. Exercises the
    full routing stack -- scope matching, dispatch, response rendering --
    exactly as a production HTTP request does, without sockets or a server.
    """
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    messages: list[dict[str, Any]] = []

    async def _receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(message: dict[str, Any]) -> None:
        messages.append(message)

    asyncio.run(app(scope, _receive, _send))

    start = next(
        message for message in messages if message["type"] == "http.response.start"
    )
    headers = {
        key.decode().lower(): value.decode() for key, value in start["headers"]
    }
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return start["status"], headers, body.decode("utf-8")


@pytest.fixture
def page() -> str:
    """Return the served dashboard document."""
    status, _headers, body = _asgi_request("/dashboard")
    assert status == 200
    return body


def _script(page_source: str) -> str:
    """Return only the page's inline JavaScript."""
    match = re.search(r"<script>(.*)</script>", page_source, re.DOTALL)
    assert match is not None
    return match.group(1)


# --- route registration and response -----------------------------------------


def test_production_app_registers_the_dashboard_route() -> None:
    """The production app's route table contains GET /dashboard.

    The regression guard established by the ``test_app_routes`` validation
    finding: a handler can pass its own tests while the production service
    returns 404, so registration is asserted on the exact served object.
    """
    matching = [
        route for route in app.routes if getattr(route, "path", None) == "/dashboard"
    ]

    assert len(matching) == 1
    assert "GET" in matching[0].methods  # type: ignore[attr-defined]


def test_dashboard_returns_200() -> None:
    """The dashboard route answers successfully."""
    status, _headers, _body = _asgi_request("/dashboard")

    assert status == 200


def test_dashboard_content_type_is_html() -> None:
    """The dashboard is served as HTML, not JSON."""
    _status, headers, _body = _asgi_request("/dashboard")

    assert headers["content-type"].startswith("text/html")


def test_dashboard_is_a_valid_document(page: str) -> None:
    """The response is a complete HTML document."""
    assert page.lstrip().startswith("<!DOCTYPE html>")
    assert "<head>" in page
    assert "<body>" in page
    assert page.rstrip().endswith("</html>")


def test_dashboard_declares_a_language(page: str) -> None:
    """The document declares its language for assistive technology."""
    assert '<html lang="en">' in page


def test_dashboard_declares_a_charset(page: str) -> None:
    """The document declares its character encoding."""
    assert '<meta charset="utf-8">' in page


def test_dashboard_declares_a_responsive_viewport(page: str) -> None:
    """The document is usable on a phone as well as a desktop."""
    assert '<meta name="viewport" content="width=device-width, ' in page


def test_dashboard_title_identifies_the_observatory(page: str) -> None:
    """The page title names the appliance it reports on."""
    match = re.search(r"<title>(.*?)</title>", page, re.DOTALL)

    assert match is not None
    assert "Matt's Garden Observatory" in match.group(1)


@pytest.mark.parametrize("heading", _REQUIRED_HEADINGS)
def test_dashboard_presents_every_required_card_heading(
    page: str, heading: str
) -> None:
    """Every mandatory card is present in the static shell."""
    assert f">{heading}</h2>" in page


@pytest.mark.parametrize("source", _REFRESH_SOURCES)
def test_dashboard_refers_to_each_supported_data_source(
    page: str, source: str
) -> None:
    """The browser refreshes from the four supported contracts."""
    assert f'"{source}"' in page


def test_dashboard_links_to_the_preview_page(page: str) -> None:
    """A normal link to the existing preview page is provided."""
    assert 'href="/preview"' in page


# --- the dashboard never controls the camera ---------------------------------


@pytest.mark.parametrize(
    "forbidden",
    [
        "/camera/preview/start",
        "/camera/preview/stop",
        "/camera/preview/stream",
        "/camera/capture",
    ],
)
def test_dashboard_never_references_a_camera_control_endpoint(
    page: str, forbidden: str
) -> None:
    """Opening the dashboard can never start, stop or stream the camera.

    The endpoint strings are absent entirely, so no code path -- and no future
    accidental edit that only changes a condition -- can reach them.
    """
    assert forbidden not in page


def test_dashboard_states_that_bird_recognition_is_not_implemented(
    page: str,
) -> None:
    """The camera placeholder must not imply identification exists."""
    assert "Bird recognition is not yet implemented." in page


# --- read-only route ---------------------------------------------------------


def test_repeated_requests_return_a_stable_shell() -> None:
    """The shell is a constant: two requests are byte-identical."""
    _s1, _h1, first = _asgi_request("/dashboard")
    _s2, _h2, second = _asgi_request("/dashboard")

    assert first == second
    assert first == render_dashboard_page()


def test_dashboard_request_does_not_mutate_application_state() -> None:
    """Requesting the dashboard attaches, replaces and removes nothing."""
    before = dict(app.state._state)

    _asgi_request("/dashboard")

    after = dict(app.state._state)
    assert set(before) == set(after)
    assert all(before[key] is after[key] for key in before)


def test_dashboard_performs_no_operational_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route does no health, hardware, database or subsystem work.

    Everything the other endpoints call to answer a request is replaced with a
    function that fails if called. The dashboard must still return 200,
    proving it serves a static shell rather than composing status itself.
    """

    def _fail(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("The dashboard route performed operational work")

    for attribute in (
        "collect_health",
        "_current_database_health",
        "_current_camera_readiness",
        "_current_motion_result",
        "_preview_service",
        "_preview_broker",
        "_notification_manager",
        "_capture_service",
        "_capture_archive",
        "apply_migrations",
        "record_observation",
        "list_observations",
        "build_identity",
    ):
        monkeypatch.setattr(app_module, attribute, _fail)

    # Subsystem-level entry points, in case a future implementation reached
    # past the application module's own helpers.
    monkeypatch.setattr(health_module, "collect_health", _fail)
    monkeypatch.setattr(health_module, "_temperature_c", _fail)
    monkeypatch.setattr(health_module.subprocess, "run", _fail)
    monkeypatch.setattr(health_module.psutil, "virtual_memory", _fail)
    monkeypatch.setattr(health_module.shutil, "disk_usage", _fail)

    status, headers, body = _asgi_request("/dashboard")

    assert status == 200
    assert headers["content-type"].startswith("text/html")
    assert "Overall health" in body


def test_dashboard_is_unaffected_by_missing_hardware_and_tooling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No camera, no thermal tooling, no Git and no monitor state is fine.

    The monitors' state holders are removed from the application entirely --
    the condition a freshly imported, lifespan-free process is in -- and the
    route is still available and complete.
    """
    for attribute in (
        "camera_state",
        "motion_state",
        "database_state",
        "preview_service",
        "preview_broker",
        "notification_manager",
        "capture_service",
        "capture_archive",
    ):
        monkeypatch.delattr(app.state, attribute, raising=False)

    status, _headers, body = _asgi_request("/dashboard")

    assert status == 200
    for heading in _REQUIRED_HEADINGS:
        assert f">{heading}</h2>" in body


# --- browser contract: safe DOM writes ---------------------------------------


def test_browser_code_writes_api_values_with_text_content(page: str) -> None:
    """API-derived values reach the DOM only through textContent."""
    script = _script(page)

    assert "textContent" in script


@pytest.mark.parametrize(
    "unsafe",
    ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("],
)
def test_browser_code_uses_no_unsafe_dom_api(page: str, unsafe: str) -> None:
    """No API-derived text can ever be parsed as markup."""
    assert unsafe not in page


def test_browser_code_maps_statuses_through_a_fixed_whitelist(page: str) -> None:
    """A status value is never interpolated into a class name."""
    script = _script(page)

    assert "STATUS_CLASSES" in script
    assert "hasOwnProperty.call(STATUS_CLASSES, value)" in script
    # The class actually assigned comes from the lookup or the neutral
    # default -- never from the API value itself.
    assert 'var className = NEUTRAL_CLASS;' in script
    assert 'node.className = "pill" + big + " " + className;' in script


def test_unknown_statuses_receive_a_neutral_fallback(page: str) -> None:
    """An unexpected future status stays visible with neutral styling."""
    script = _script(page)

    assert 'NEUTRAL_CLASS = "s-neutral"' in script
    # The status text is still rendered, so the value is never hidden.
    assert 'node.textContent = glyph + " " + formatText(value);' in script


def test_status_is_not_communicated_by_colour_alone(page: str) -> None:
    """Every status pill carries a textual glyph as well as a colour."""
    script = _script(page)

    assert "STATUS_GLYPHS" in script
    assert "colour alone" in script


# --- browser contract: refresh loop ------------------------------------------


def test_refresh_interval_is_bounded_and_sensible(page: str) -> None:
    """The poll interval is a single named constant in a sane range."""
    match = re.search(r"var REFRESH_MS = (\d+);", _script(page))

    assert match is not None
    interval_ms = int(match.group(1))
    assert interval_ms == 10000
    assert 1000 <= interval_ms <= 60000


def test_an_immediate_first_refresh_happens_on_load(page: str) -> None:
    """The page does not wait a full interval before showing data."""
    script = _script(page)

    assert re.search(r"renderSummary\(\);\s*refresh\(\);\s*\}\)\(\);", script)


def test_refresh_cycles_cannot_overlap(page: str) -> None:
    """A completion-scheduled loop with a re-entrancy guard, not setInterval."""
    script = _script(page)

    assert "setInterval" not in script
    assert "if (refreshing) {\n      return;\n    }" in script
    assert "timer = window.setTimeout(refresh, REFRESH_MS);" in script
    # The next cycle is only scheduled after the previous one settles.
    assert "refreshing = false;\n      schedule();" in script


def test_partial_failure_is_handled_explicitly(page: str) -> None:
    """One failed endpoint never discards the other three's responses."""
    script = _script(page)

    assert "Promise.allSettled" in script
    assert 'result.status === "fulfilled"' in script
    assert "Partial " in script


def test_a_failed_source_is_degraded_and_keeps_its_last_reading(
    page: str,
) -> None:
    """A failure degrades the card's badge; it never clears the values."""
    script = _script(page)

    # Failure goes through the single transition helper, never a direct
    # assignment at the call site.
    assert "markSourceFailure(key);\n        failures += 1;" in script
    assert "Stale " in script
    assert "showing the last " in script
    # Nothing in the failure path writes a placeholder over a rendered card:
    # only the badge elements are touched.
    assert "markAllFailed" in script


# --- browser contract: the four-state refresh model --------------------------
#
# The state a failed source lands in depends on whether it has EVER succeeded.
# A source that has never returned a reading must say so, and must never claim
# to be showing a last successful reading it does not have.


def test_state_vocabulary_includes_unavailable(page: str) -> None:
    """The model distinguishes never, unavailable, loaded and stale."""
    script = _script(page)

    for state in ('"never"', '"unavailable"', '"loaded"', '"stale"'):
        assert state in script

    # All four are rendered as distinct badges.
    assert 'if (state === "never") {' in script
    assert '} else if (state === "unavailable") {' in script
    assert '} else if (state === "stale") {' in script


def test_unavailable_wording_denies_any_successful_reading(page: str) -> None:
    """A never-loaded source states plainly that no reading exists yet."""
    script = _script(page)

    assert "Unavailable " in script
    assert "no successful " in script
    assert "reading yet" in script


def test_never_loaded_failure_does_not_become_stale(page: str) -> None:
    """`never` degrades to `unavailable`, never straight to `stale`.

    This is the corrective regression guard: the earlier implementation
    assigned "stale" unconditionally, so a first-load failure claimed to be
    showing a last successful reading that had never been fetched.
    """
    script = _script(page)

    # There is no unconditional generic failure assignment anywhere.
    assert 'sourceState[key] = "stale";' not in script
    assert 'sourceState[SOURCE_KEYS[index]] = "stale";' not in script

    # The one assignment is a variable whose default is "unavailable" and
    # which only becomes "stale" when a previous success is proven.
    assert 'var degraded = "unavailable";' in script
    assert (
        'if (previous === "loaded" || previous === "stale") {\n'
        '      degraded = "stale";\n'
        "    }\n"
        "    sourceState[key] = degraded;" in script
    )


def test_failure_transitions_depend_on_the_previous_state(page: str) -> None:
    """never/unavailable stay unavailable; loaded/stale become stale."""
    script = _script(page)

    match = re.search(
        r"function markSourceFailure\(key\) \{(.*?)\n  \}", script, re.DOTALL
    )
    assert match is not None
    body = match.group(1)

    # The previous state is what decides the outcome.
    assert "var previous = sourceState[key];" in body
    # Only a proven prior success (loaded or stale) yields stale.
    assert '"loaded"' in body
    assert '"stale"' in body
    assert '"unavailable"' in body
    # "never" is not in the promoting branch, so it falls through to
    # unavailable rather than being special-cased into stale.
    assert '"never"' not in body


def test_all_source_failure_uses_the_same_transition_helper(page: str) -> None:
    """The catch-all path cannot bypass the per-state transition rule."""
    script = _script(page)

    match = re.search(
        r"function markAllFailed\(\) \{(.*?)\n  \}", script, re.DOTALL
    )
    assert match is not None
    assert "markSourceFailure(SOURCE_KEYS[index]);" in match.group(1)

    # The catch path for a wholly failed refresh uses it too.
    assert "markAllFailed();" in script


def test_total_failure_summary_does_not_claim_stale_readings(page: str) -> None:
    """With nothing ever loaded, the summary must not imply prior data."""
    script = _script(page)

    assert 'lastOutcome = "Failed \\u2014 no source answered";' in script
    assert "readings are stale" not in script


# --- browser contract: payload validation ------------------------------------


def test_source_specific_payload_validators_exist(page: str) -> None:
    """Each endpoint has its own minimum-shape validator."""
    script = _script(page)

    for validator in (
        "function validateHealth(",
        "function validateVersion(",
        "function validateMotion(",
        "function validateNotifications(",
    ):
        assert validator in script

    assert "var VALIDATORS = {" in script
    for key in ('"health":', '"version":', '"motion":', '"notifications":'):
        assert key in script


def test_validation_happens_before_the_renderer_is_called(page: str) -> None:
    """No card may be mutated by a payload that failed validation."""
    script = _script(page)

    match = re.search(
        r"function apply\(key, payload\) \{(.*?)\n  \}", script, re.DOTALL
    )
    assert match is not None
    body = match.group(1)

    validate_at = body.index("VALIDATORS[key](payload)")
    render_at = body.index("RENDERERS[key](payload)")
    assert validate_at < render_at

    # A failed validation returns immediately, without rendering.
    assert (
        "if (VALIDATORS[key](payload) !== true) {\n      return false;\n    }" in body
    )


@pytest.mark.parametrize(
    ("constant", "fields"),
    [
        (
            "HEALTH_FIELDS",
            (
                "status",
                "application",
                "hostname",
                "uptime_seconds",
                "cpu_percent",
                "memory",
                "disk",
                "temperature",
                "database",
                "camera",
                "preview",
            ),
        ),
        (
            "VERSION_FIELDS",
            ("application", "version", "commit", "python_version", "architecture"),
        ),
        (
            "MOTION_FIELDS",
            (
                "enabled",
                "status",
                "detected",
                "score",
                "threshold",
                "frames_available",
                "detail",
                "evaluated_at",
            ),
        ),
        (
            "NOTIFICATION_FIELDS",
            (
                "enabled",
                "providers",
                "total_events_published",
                "total_delivery_failures",
                "last_event_at",
            ),
        ),
    ],
)
def test_validators_require_every_contract_field(
    page: str, constant: str, fields: tuple[str, ...]
) -> None:
    """The validators pin each endpoint's required contract fields."""
    script = _script(page)

    match = re.search(rf"var {constant} = \[(.*?)\];", script, re.DOTALL)
    assert match is not None
    declared = set(re.findall(r'"([a-z_]+)"', match.group(1)))

    assert declared == set(fields)


def test_health_validator_requires_object_sections(page: str) -> None:
    """A health payload whose sections are not objects is not usable."""
    script = _script(page)

    match = re.search(r"var HEALTH_SECTIONS = \[(.*?)\];", script, re.DOTALL)
    assert match is not None
    assert set(re.findall(r'"([a-z_]+)"', match.group(1))) == {
        "memory",
        "disk",
        "temperature",
        "database",
        "camera",
        "preview",
    }
    assert "if (!isObject(payload[HEALTH_SECTIONS[index]])) {" in script


def test_validators_reject_non_objects_and_arrays(page: str) -> None:
    """A scalar, null or array body is not this endpoint's response."""
    script = _script(page)

    assert 'value !== null && typeof value === "object"' in script
    assert 'Object.prototype.toString.call(value) !== "[object Array]"' in script
    assert "if (!isObject(payload)) {\n      return false;\n    }" in script


def test_validators_allow_nullable_api_fields(page: str) -> None:
    """Presence is checked, not truthiness, so documented nulls stay valid.

    `temperature.celsius`, `commit`, `preview.owner`, `preview.uptime_seconds`
    and `last_event_at` are all legitimately null, and a zero counter is a
    real reading. Validation must not reject any of them.
    """
    script = _script(page)

    match = re.search(
        r"function hasFields\(payload, names\) \{(.*?)\n  \}", script, re.DOTALL
    )
    assert match is not None
    body = match.group(1)

    assert "hasOwnProperty.call(payload, names[index])" in body
    # No value inspection at all -- only key presence.
    assert "typeof payload[" not in body
    assert "=== null" not in body


def test_refresh_summary_reports_attempt_completeness_and_success(
    page: str,
) -> None:
    """The page truthfully reports what the latest refresh achieved."""
    assert 'id="refresh-attempt"' in page
    assert 'id="refresh-complete"' in page
    assert 'id="refresh-outcome"' in page

    script = _script(page)
    assert "lastComplete = lastAttempt;" in script
    assert "Complete " in script
    assert "Failed " in script


def test_polling_pauses_while_the_page_is_hidden(page: str) -> None:
    """Hidden tabs stop polling and refresh immediately when shown again."""
    script = _script(page)

    assert '"visibilitychange"' in script
    assert "document.hidden === true" in script


@pytest.mark.parametrize(
    "streaming", ["WebSocket", "EventSource", "SharedWorker"]
)
def test_no_streaming_protocol_is_used(page: str, streaming: str) -> None:
    """Refresh is plain polling; no socket or event stream is opened."""
    assert streaming not in page


# --- browser contract: read-only and self-contained --------------------------


def test_browser_code_performs_only_get_requests(page: str) -> None:
    """There is no browser-generated write operation of any kind."""
    script = _script(page)

    assert 'method: "GET"' in script
    for method in ('"POST"', '"PUT"', '"PATCH"', '"DELETE"'):
        assert method not in script
    # Every fetch call is the single shared helper, which is GET-only.
    assert script.count("fetch(") == 1


@pytest.mark.parametrize(
    "forbidden",
    [
        "vcgencmd",
        "/proc",
        "psutil",
        "sqlite",
        "SQLite",
        "rpicam",
        "libcamera",
        "subprocess",
    ],
)
def test_browser_code_attempts_no_hardware_or_database_work(
    page: str, forbidden: str
) -> None:
    """The browser reads API contracts only; it inspects nothing itself."""
    assert forbidden not in _script(page)


@pytest.mark.parametrize(
    "forbidden",
    ["http://", "https://", "//cdn", "<link", "crossorigin", "integrity=", "url("],
)
def test_page_has_no_external_asset_dependency(page: str, forbidden: str) -> None:
    """The appliance dashboard works with no internet access at all."""
    assert forbidden not in page


def test_all_styles_and_scripts_are_inline(page: str) -> None:
    """One document, no second request for CSS or JavaScript."""
    assert "<style>" in page
    assert "<script>" in page
    assert "<script src" not in page


def test_javascript_disabled_message_is_present(page: str) -> None:
    """A truthful noscript fallback, asserting no state."""
    match = re.search(r"<noscript>(.*?)</noscript>", page, re.DOTALL)

    assert match is not None
    message = match.group(1)
    assert "JavaScript is disabled" in message
    assert "no status is shown or implied" in message
    assert 'href="/preview"' in message


def test_static_shell_fabricates_no_healthy_state(page: str) -> None:
    """Before any API response the page asserts nothing about the system."""
    # The shell's only pre-refresh words are neutral placeholders.
    assert "Loading" in page
    assert "Awaiting live data" in page
    assert "Not yet loaded" in page

    # No *value slot* claims a positive state in the served markup. Every
    # one of these words only ever appears as a rendered value, produced from
    # an actual API response. (Field labels such as "Available" are headings,
    # not claims, so only the value slots are inspected.)
    shell = page.replace(_script(page), "")
    slots = re.findall(r"<dd[^>]*>(.*?)</dd>", shell, re.DOTALL)
    overall = re.search(r'id="overall-status">(.*?)</span>', shell, re.DOTALL)
    assert overall is not None
    slots.append(overall.group(1))

    assert len(slots) > 20
    for slot in slots:
        for fabricated in ("Healthy", "healthy", "Available", "Running", "Enabled"):
            assert fabricated not in slot


# --- browser contract: formatting safety -------------------------------------


@pytest.mark.parametrize("pattern", ['|| "', "|| '", "|| 0", "?? "])
def test_zero_is_never_treated_as_missing(page: str, pattern: str) -> None:
    """No truthiness-based fallback can turn a valid 0 into "unavailable"."""
    assert pattern not in _script(page)


def test_formatters_check_explicitly_for_null_and_finite_numbers(
    page: str,
) -> None:
    """Absence is detected by explicit checks, not by falsiness."""
    script = _script(page)

    assert 'typeof value === "number" && isFinite(value)' in script
    assert "value === null || value === undefined" in script
    assert 'typeof value !== "string"' in script


def test_formatters_cover_the_required_value_kinds(page: str) -> None:
    """Uptime, percentages, bytes, timestamps, flags and lists are formatted."""
    script = _script(page)

    for helper in (
        "function formatDuration(",
        "function formatPercent(",
        "function formatBytes(",
        "function formatTimestamp(",
        "function formatFlag(",
        "function formatProviders(",
        "function formatCelsius(",
        "function formatCount(",
    ):
        assert helper in script


def test_byte_sizes_use_documented_binary_units(page: str) -> None:
    """The chosen unit convention is binary, and it is stated on the page."""
    script = _script(page)

    assert 'BYTE_UNITS = ["bytes", "KiB", "MiB", "GiB", "TiB", "PiB"]' in script
    assert "size = size / 1024;" in script
    assert "binary units (KiB, MiB, GiB, TiB)" in page


def test_used_bytes_are_derived_for_display_only(page: str) -> None:
    """Memory and disk used-bytes are derived, never invented as API fields."""
    script = _script(page)

    assert "function derivedUsedBytes(total, remaining)" in script
    assert "var used = total - remaining;" in script
    # A nonsensical pair (available greater than total) is not displayed.
    assert "if (used < 0) {" in script
    # No fabricated API field name is read.
    assert "used_bytes" not in script
    assert "derived for display only" in page


def test_temperature_absence_is_truthful_not_zero(page: str) -> None:
    """A null temperature reads as "not reported", never as 0 degrees."""
    script = _script(page)

    assert "function formatCelsius(value) {\n    if (!isNumber(value)) {\n" in script
    assert 'NOT_REPORTED = "Not reported"' in script


def test_disabled_states_are_not_presented_as_failures(page: str) -> None:
    """Disabled, stopped and unconfigured are normal, not faults."""
    script = _script(page)

    # "disabled" maps to the neutral class, never to the critical one.
    assert '"disabled": "s-neutral"' in script
    assert '"stopped": "s-neutral"' in script
    assert '"no_motion": "s-healthy"' in script
    # An empty provider list is reported truthfully.
    assert 'return "None configured";' in script
    assert "Not running" in script
    assert "No event yet" in script


def test_motion_is_not_described_as_recognition(page: str) -> None:
    """Scene change must never be presented as bird or object recognition."""
    assert "scene change" in page.lower()
    assert "not object recognition" in page
    assert "not species" in page


def test_motion_score_is_only_shown_when_measurable(page: str) -> None:
    """Score is a measurement only when a frame was actually available."""
    script = _script(page)

    assert "if (frames === true) {" in script
    assert 'setText("motion-score", "Not measured");' in script


# --- privacy -----------------------------------------------------------------


@pytest.mark.parametrize(
    "secret",
    [
        "/etc/garden-observatory",
        "/var/lib/garden-observatory",
        "/var/log/garden-observatory",
        "mgo.db",
        "mgo.toml",
        "data/captures",
        "MGO_CONFIG_PATH",
        "MGO_BUILD_COMMIT",
        ".git",
        "github.com",
        "C:\\",
        "Traceback",
        "password",
        "token",
        "secret",
    ],
)
def test_dashboard_exposes_no_path_secret_or_environment_value(
    page: str, secret: str
) -> None:
    """Only fields the API contracts deliberately publish may appear."""
    assert secret not in page


def test_dashboard_exposes_no_hard_coded_deployment_identity(page: str) -> None:
    """No Pi hostname or production address is baked into the page."""
    assert "mgo-core" not in page
    assert "192.168." not in page
    assert "127.0.0.1" not in page
    assert ":8080" not in page


def test_the_supported_endpoint_paths_are_still_present(page: str) -> None:
    """The privacy rules must not remove the legitimate fixed API paths."""
    for path in (*_REFRESH_SOURCES, "/preview"):
        assert path in page


# --- existing contracts are unchanged by adding /dashboard -------------------
#
# These complement, and do not replace, the Task 8 contract assertions in
# tests/test_version_api.py. They prove the *addition* of /dashboard changed
# nothing about the endpoints the dashboard consumes and the page it links to.


def test_root_still_returns_exactly_its_three_keys() -> None:
    """Task 8 protects `/` as the minimal JSON identity endpoint."""
    status, headers, body = _asgi_request("/")

    assert status == 200
    assert headers["content-type"].startswith("application/json")
    assert set(json.loads(body)) == {"name", "version", "status"}


def test_root_is_not_redirected_to_the_dashboard() -> None:
    """`/` is not replaced, redirected or repurposed by the dashboard."""
    status, _headers, body = _asgi_request("/")

    assert status == 200
    assert "<html" not in body
    assert "dashboard" not in body.lower()


def test_version_still_returns_its_exact_fields() -> None:
    """`/version` is untouched by this task."""
    status, headers, body = _asgi_request("/version")

    assert status == 200
    assert headers["content-type"].startswith("application/json")
    assert set(json.loads(body)) == {
        "application",
        "version",
        "commit",
        "python_version",
        "architecture",
    }


def test_health_still_returns_every_protected_top_level_field() -> None:
    """`/health` keeps the full contract Task 8 formalised."""
    status, headers, body = _asgi_request("/health")

    assert status == 200
    assert headers["content-type"].startswith("application/json")
    payload = json.loads(body)
    assert set(payload) == {
        "status",
        "application",
        "hostname",
        "architecture",
        "python_version",
        "uptime_seconds",
        "cpu_percent",
        "memory",
        "disk",
        "temperature",
        "database",
        "camera",
        "preview",
    }
    # The exact section shapes the dashboard reads.
    assert set(payload["memory"]) == {
        "total_bytes",
        "available_bytes",
        "used_percent",
        "status",
    }
    assert set(payload["disk"]) == {
        "total_bytes",
        "free_bytes",
        "used_percent",
        "status",
    }
    assert set(payload["temperature"]) == {"celsius", "status"}
    assert set(payload["preview"]) == {
        "enabled",
        "state",
        "owner",
        "uptime_seconds",
    }


def test_preview_page_still_serves_its_own_html_and_controls() -> None:
    """`/preview` keeps its page, its controls and its endpoint references."""
    status, headers, body = _asgi_request("/preview")

    assert status == 200
    assert headers["content-type"].startswith("text/html")
    assert "Live Preview" in body
    assert "Start Preview" in body
    assert "Stop Preview" in body
    for endpoint in (
        "/camera/preview/status",
        "/camera/preview/start",
        "/camera/preview/stop",
        "/camera/preview/stream",
    ):
        assert endpoint in body


def test_dashboard_and_preview_remain_separate_pages() -> None:
    """The dashboard does not duplicate or replace the preview page."""
    _s1, _h1, dashboard = _asgi_request("/dashboard")
    _s2, _h2, preview = _asgi_request("/preview")

    assert dashboard != preview
    assert "Start Preview" not in dashboard
    assert "Overall health" not in preview
