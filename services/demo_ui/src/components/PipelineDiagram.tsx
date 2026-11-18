// Animated pipeline diagram (task 9.10): one node per step, edges animate into the active step.
import { Background, Position, ReactFlow, type Edge, type Node } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useTranslation } from "react-i18next";
import type { StepId, StepView } from "@/lib/pipeline";

const TONE: Record<string, string> = {
  done: "hsl(var(--success))",
  active: "hsl(var(--primary))",
  failed: "hsl(var(--danger))",
  skipped: "hsl(var(--muted-foreground))",
  idle: "hsl(var(--border))",
};

export function PipelineDiagram({
  steps,
  selected,
  onSelect,
}: {
  steps: StepView[];
  selected: StepId | null;
  onSelect: (id: StepId) => void;
}) {
  const { t } = useTranslation();
  const nodes: Node[] = steps.map((s, i) => {
    const row = Math.floor(i / 5);
    const col = row % 2 === 0 ? i % 5 : 4 - (i % 5); // snake: row 2 runs right-to-left
    const lastInRow = i % 5 === 4;
    const rtl = row % 2 === 1;
    return {
    id: s.id,
    position: { x: col * 190, y: row * 110 },
    sourcePosition: lastInRow ? Position.Bottom : rtl ? Position.Left : Position.Right,
    targetPosition: i % 5 === 0 && row > 0 ? Position.Top : rtl ? Position.Right : Position.Left,
    data: { label: `${t(`pipeline.steps.${s.id}`)}${s.status === "skipped" ? " ⤼" : ""}` },
    draggable: false,
    style: {
      width: 160,
      borderRadius: 10,
      border: `2px solid ${TONE[s.status]}`,
      background: "hsl(var(--card))",
      color: "hsl(var(--card-foreground))",
      opacity: s.status === "skipped" ? 0.45 : 1,
      boxShadow: selected === s.id ? `0 0 0 3px ${TONE.active}` : undefined,
      fontSize: 13,
    },
    };
  });
  const edges: Edge[] = steps.slice(1).map((s, i) => ({
    id: `${steps[i].id}-${s.id}`,
    source: steps[i].id,
    target: s.id,
    type: "smoothstep",
    animated: s.status === "active",
    style: { stroke: TONE[s.status === "idle" ? "idle" : steps[i].status], strokeWidth: 2 },
  }));

  return (
    <div className="h-64 rounded-lg border bg-background" aria-label="pipeline">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        fitView
        nodesConnectable={false}
        zoomOnScroll={false}
        panOnDrag={false}
        proOptions={{ hideAttribution: true }}
        onNodeClick={(_, node) => onSelect(node.id as StepId)}
      >
        <Background gap={24} />
      </ReactFlow>
    </div>
  );
}
