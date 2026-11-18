"""Demo console end to end in a browser (task 9.15, R25.7, R24.5).

Runs against a stack started with the fake profile (``LLM__PROVIDER=fake``, ``DEMO__ENABLED=true``,
``make seed``) and the console at ``DEMO_CONSOLE_URL`` (e.g. ``http://localhost:3003``). Skipped
without it, so CI's review-UI job is not affected. No live credentials: every model call is fake.
"""

from __future__ import annotations

import os
import re

import pytest

URL = os.environ.get("DEMO_CONSOLE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="set DEMO_CONSOLE_URL to a running demo console")

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def page():  # type: ignore[no-untyped-def]
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        yield context.new_page()
        browser.close()


def test_the_console_loads_in_both_languages(page) -> None:  # type: ignore[no-untyped-def]
    page.goto(f"{URL}/scenarios")
    page.get_by_text("S01").first.wait_for()
    page.get_by_role("button", name=re.compile("^(EN|VI)$")).click()
    page.get_by_text("S01").first.wait_for()


def test_s01_runs_to_an_early_exit_and_passes(page) -> None:  # type: ignore[no-untyped-def]
    page.goto(f"{URL}/scenarios")
    card = (
        page.locator("div", has=page.get_by_text("S01", exact=True))
        .filter(has=page.get_by_role("button", name=re.compile("^(Chạy|Run)$")))
        .last
    )
    card.get_by_role("button", name=re.compile("^(Chạy|Run)$")).click()
    page.get_by_text(re.compile("^(Đạt|Pass)$")).first.wait_for(timeout=60_000)
    page.get_by_text("COMPLETED").first.wait_for()


def test_the_pipeline_diagram_shows_the_run(page) -> None:  # type: ignore[no-untyped-def]
    page.goto(f"{URL}/")
    page.locator("[aria-label=pipeline]").wait_for()
    page.screenshot(path="demo-console-pipeline.png", full_page=True)
