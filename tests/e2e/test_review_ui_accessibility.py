"""WCAG 2.2 AA basics on the draft review screen (task 6.8).

2.4.7 / 2.4.11 visible focus, 2.5.8 target size (minimum 24 x 24 CSS px; inline links in
text are exempt), 4.1.3 status messages through a polite live region, 2.4.3 focus order
after an htmx swap.
"""

from __future__ import annotations

from typing import Any

from playwright.sync_api import Page, expect

from tests.e2e.review_stack import SUBJECT, ReviewStack, seed_pending_draft

FOCUS_PROBE = """() => {
  const el = document.activeElement;
  const style = getComputedStyle(el);
  return {
    label: (el.textContent || "").trim() || el.getAttribute("name") || el.id || el.tagName,
    outline: style.outlineStyle,
    width: parseFloat(style.outlineWidth),
  };
}"""

TARGETS_PROBE = """els => els.map(el => {
  const box = el.getBoundingClientRect();
  const inline = el.tagName === "A" && el.closest("p, li, dd, td") !== null;
  return {
    label: (el.textContent || "").trim() || el.getAttribute("name") || el.id,
    width: box.width,
    height: box.height,
    inline: inline,
  };
})"""


def _open_seeded_draft(page: Page, stack: ReviewStack) -> None:
    seeded = stack.run(seed_pending_draft(stack.db, stack.organization_id))
    page.goto(f"{stack.url}/drafts/{seeded.draft_id}")
    expect(page.get_by_role("heading", level=1)).to_have_text(SUBJECT)


def test_every_control_reached_by_tab_shows_a_visible_focus_indicator(
    page: Page, stack: ReviewStack
) -> None:
    _open_seeded_draft(page, stack)
    seen: list[dict[str, Any]] = []
    for _ in range(40):
        page.keyboard.press("Tab")
        probe: dict[str, Any] = page.evaluate(FOCUS_PROBE)
        seen.append(probe)
        if probe["label"] == "Reject":
            break
    labels = [p["label"] for p in seen]
    assert "Skip to main content" in labels
    assert "Approve" in labels and "Reject" in labels
    invisible = [p for p in seen if p["outline"] == "none" or p["width"] < 2]
    assert not invisible, f"focus without a visible outline: {invisible}"


def test_interactive_targets_are_at_least_24_px(page: Page, stack: ReviewStack) -> None:
    _open_seeded_draft(page, stack)
    boxes: list[dict[str, Any]] = page.eval_on_selector_all(
        "header a, main a, main button, main input:not([type=hidden]), main textarea",
        TARGETS_PROBE,
    )
    assert len(boxes) >= 6
    too_small = [b for b in boxes if not b["inline"] and (b["width"] < 24 or b["height"] < 24)]
    assert not too_small, f"targets under 24 px: {too_small}"


def test_status_messages_use_a_polite_live_region_and_focus_returns_to_the_panel(
    page: Page, stack: ReviewStack
) -> None:
    _open_seeded_draft(page, stack)
    region = page.get_by_role("status")
    expect(region).to_have_attribute("aria-live", "polite")

    page.get_by_role("button", name="Save changes").click()

    expect(region).to_have_text("Changes saved.")
    expect(page.locator("#draft-heading")).to_be_focused()
