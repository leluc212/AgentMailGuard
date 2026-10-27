r"""Pre-demo browser check of the running Playground at 1280x720 (Replay only, no API call).

Start the Playground first, then run:  .venv\Scripts\python.exe scripts\playground\ui_check.py
Screenshots go to %TEMP%\guard_playground_ui_check. Exit status 1 lists every failed check.

Checks Requirements 10.3 (no horizontal scroll, two columns side by side), 10.6 and 19.3
(font sizes), 13.8 (stepper fits), 17 (tour), 19 (presenter mode), 10.8 (browser zoom 125-400%
reflows without horizontal scrolling) and that no JS error occurs.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright

BASE = "http://127.0.0.1:3010"
OUT = Path(tempfile.gettempdir()) / "guard_playground_ui_check"
BODY_SIZE_JS = "parseFloat(getComputedStyle(document.querySelector('.draft, .column p')).fontSize)"
HEADING_SIZE_JS = "parseFloat(getComputedStyle(document.querySelector('h1')).fontSize)"
problems: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        problems.append(msg)


def layout(page: Page, tag: str) -> tuple[float, float]:
    scroll_w = page.evaluate("document.documentElement.scrollWidth")
    check(scroll_w <= 1280, f"[{tag}] no horizontal scroll (scrollWidth={scroll_w})")
    boxes = [c.bounding_box() for c in page.locator(".columns .column").all()]
    if len(boxes) == 2 and all(boxes):
        a, b = boxes[0], boxes[1]
        assert a is not None and b is not None
        side = abs(a["y"] - b["y"]) < 5 and a["x"] < b["x"]
        check(side and b["x"] + b["width"] <= 1280, f"[{tag}] two columns side by side")
    for stepper in page.locator(".stepper").all():
        sb = stepper.bounding_box()
        col = stepper.locator("xpath=ancestor::article[1]").bounding_box()
        if sb and col:
            fits = sb["x"] + sb["width"] <= col["x"] + col["width"] + 1
            check(fits, f"[{tag}] stepper fits its column")
            break
    return page.evaluate(BODY_SIZE_JS), page.evaluate(HEADING_SIZE_JS)


ZOOMS = (1.25, 1.5, 2.0, 3.0, 4.0)
SIDE_BY_SIDE_UNTIL = 1.25  # above this zoom the two result columns stack (C0 above C3)


def check_zoom(browser: Any, zoom: float) -> None:
    tag = f"zoom {int(zoom * 100)}%"
    page = browser.new_page(viewport={"width": int(1280 / zoom), "height": int(720 / zoom)})
    page.goto(BASE + "/guard?case_id=attack-llmail-0a41d5272ecd")
    page.wait_for_load_state("networkidle")
    page.click("#run-status button")
    page.wait_for_selector(".column .banner__text", timeout=5000)
    width = page.evaluate("document.documentElement.scrollWidth")
    client = page.evaluate("document.documentElement.clientWidth")
    check(width <= client, f"[{tag}] /guard reflows without horizontal scroll ({width}/{client})")
    tops = page.evaluate(
        "[...document.querySelectorAll('.columns .column')].map(c => c.getBoundingClientRect().top)"
    )
    side = len(tops) == 2 and abs(tops[0] - tops[1]) < 5
    expected = zoom <= SIDE_BY_SIDE_UNTIL
    check(side == expected, f"[{tag}] columns {'side by side' if expected else 'stacked'}")
    page.screenshot(path=str(OUT / f"zoom{int(zoom * 100)}.png"))
    page.goto(BASE + "/guard/results")
    page.wait_for_load_state("networkidle")
    width = page.evaluate("document.documentElement.scrollWidth")
    check(width <= client, f"[{tag}] /guard/results reflows ({width}/{client})")
    page.close()


def main() -> int:
    OUT.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        page.goto(BASE + "/guard")
        page.wait_for_load_state("networkidle")
        page.click("#tour-start")
        page.wait_for_timeout(400)
        check(page.inner_text("#tour-count") == "Step 1 of 5", "tour starts at step 1 of 5")
        page.keyboard.press("n")
        page.wait_for_timeout(600)
        check(page.inner_text("#tour-count") == "Step 2 of 5", "N key moves to step 2")
        check("attack-llmail-0a41d5272ecd" in page.inner_text("#case-detail"), "step 2 case")
        page.click("#run-status button")
        page.wait_for_selector(".column .banner__text", timeout=5000)
        page.wait_for_timeout(300)
        banners = page.locator(".banner__text").all_inner_texts()
        check(banners == ["NO GUARD DECISION", "QUARANTINED"], f"banners C0/C3: {banners}")

        size, heading = layout(page, "normal")
        check(size >= 18 and heading >= 24, f"[normal] body {size}px, heading {heading}px")
        page.screenshot(path=str(OUT / "normal.png"))
        page.locator(".stage-link").first.click()

        page.click("#presenter-toggle")
        page.wait_for_timeout(300)
        check(page.get_attribute("#presenter-toggle", "aria-pressed") == "true", "presenter on")
        size, heading = layout(page, "presenter")
        check(size >= 20 and heading >= 26, f"[presenter] body {size}px, heading {heading}px")
        collapsed = page.evaluate(
            "[...document.querySelectorAll('details.prompt')].every(d => !d.open)"
        )
        check(collapsed, "[presenter] guarded prompts collapsed")
        page.screenshot(path=str(OUT / "presenter.png"))

        page.goto(BASE + "/guard/results")
        page.wait_for_load_state("networkidle")
        imgs = page.evaluate("[...document.images].map(i => i.complete && i.naturalWidth > 0)")
        check(len(imgs) == 6 and all(imgs), f"results page: {imgs.count(True)}/6 figures loaded")
        width = page.evaluate("document.documentElement.scrollWidth")
        check(width <= 1280, f"[results] no horizontal scroll (scrollWidth={width})")
        page.screenshot(path=str(OUT / "results.png"))

        check(not errors, f"no JS errors {json.dumps(errors)[:300]}")
        page.close()

        # Browser zoom on a 1280x720 screen = a narrower CSS viewport (WCAG 1.4.10 Reflow).
        for zoom in ZOOMS:
            check_zoom(browser, zoom)
        browser.close()
    print(f"\n{len(problems)} problem(s); screenshots in {OUT}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
