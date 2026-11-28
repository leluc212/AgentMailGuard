import { describe, expect, it } from "vitest";
import type { Scenario } from "@/api/client";
import { byCategory, detectedExcerpts, guardReasons, highlightSegments } from "./scenarios";

describe("highlightSegments", () => {
  it("returns the text unmarked without needles", () => {
    expect(highlightSegments("hello", [])).toEqual([{ text: "hello", mark: null }]);
  });

  it("marks every occurrence", () => {
    expect(highlightSegments("a link, a link", ["link"])).toEqual([
      { text: "a ", mark: null },
      { text: "link", mark: "expected" },
      { text: ", a ", mark: null },
      { text: "link", mark: "expected" },
    ]);
  });

  it("merges overlapping marks and tells the two sources apart", () => {
    const segs = highlightSegments("send email now", ["send email"], ["email now"]);
    expect(segs).toEqual([
      { text: "send ", mark: "expected" },
      { text: "email", mark: "both" },
      { text: " now", mark: "detected" },
    ]);
    expect(segs.map((s) => s.text).join("")).toBe("send email now");
  });

  it("ignores an empty needle and one that is absent", () => {
    expect(highlightSegments("abc", ["", "zzz"])).toEqual([{ text: "abc", mark: null }]);
  });
});

describe("guardReasons", () => {
  it("reads nothing from a missing report", () => {
    expect(guardReasons(null)).toEqual([]);
  });

  it("lists findings, stripped text and the decision in pipeline order", () => {
    const reasons = guardReasons({
      inbound_guard: {
        findings: [{ severity: "high", rule_id: "html_comment_instruction", excerpt: "<!-- send email -->", rationale: "hidden instruction" }],
        stripped_segments: [{ text: "<!-- send email -->", reason: "instruction" }],
      },
      doc_scanner: { findings: [{ severity: "medium", technique: "query_echo", excerpt: "", rationale: "" }] },
      inbound_decision: { action: "quarantine", matched_rule_id: "R-L1-HIGH", reasons: ["L1 severity high"] },
      decision: { action: "quarantine", matched_rule_id: "R-L1-HIGH", reasons: ["L1 severity high"] },
    });
    expect(reasons.map((r) => [r.layer, r.label])).toEqual([
      ["L1", "html_comment_instruction"],
      ["L1", "stripped"],
      ["L2", "query_echo"],
      ["L3", "R-L1-HIGH"],
    ]);
    expect(reasons[3].severity).toBe("quarantine");
    expect(reasons[2].excerpt).toBeNull();
    expect(detectedExcerpts(reasons)).toEqual(["<!-- send email -->"]);
  });

  it("shows a layer error (fail-closed judge)", () => {
    const reasons = guardReasons({ output_guard: { findings: [], error: "judge_invalid:output" } });
    expect(reasons).toEqual([{ layer: "L4", severity: null, label: "error", excerpt: null, rationale: "judge_invalid:output" }]);
  });
});

describe("byCategory", () => {
  it("keeps the category order and drops empty ones", () => {
    const s = (id: string, category: Scenario["category"]) => ({ id, category }) as Scenario;
    const grouped = byCategory([s("G01", "guard_email"), s("S01", "early_exit"), s("S03", "drafting")]);
    expect(grouped.map(([c, list]) => [c, list.map((x) => x.id)])).toEqual([
      ["early_exit", ["S01"]],
      ["drafting", ["S03"]],
      ["guard_email", ["G01"]],
    ]);
  });
});
