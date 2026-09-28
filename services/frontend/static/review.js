// Review UI helpers (task 6.8; R16.7, WCAG 2.2 AA 2.4.3).
"use strict";

// Milliseconds from page render to the decision, sent with approve/reject as review_ms
// through hx-vals='js:{review_ms: reviewElapsedMs()}'.
const reviewStartedAt = performance.now();

function reviewElapsedMs() {
  return Math.max(0, Math.round(performance.now() - reviewStartedAt));
}
window.reviewElapsedMs = reviewElapsedMs;

// After htmx replaces the draft panel the focused button is gone; put focus on the
// panel heading so keyboard and screen-reader users keep their place.
document.addEventListener("htmx:afterSettle", () => {
  const active = document.activeElement;
  if (active && active !== document.body) {
    return;
  }
  const heading = document.getElementById("draft-heading");
  if (heading) {
    heading.focus();
  }
});
