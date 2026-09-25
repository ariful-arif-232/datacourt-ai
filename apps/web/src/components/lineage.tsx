"use client";

import { useMemo } from "react";
import { Badge, KindTag, SampleThumb, SplitTag } from "@/components/ui";
import { fixed, humanize } from "@/lib/format";

type Node = { id: string; parent: string | null; relation: string | null; similarity: number | null; depth: number; evidence: any; sample: any };

const NODE_W = 132;
const NODE_H = 150;
const GAP_X = 72;
const GAP_Y = 22;

/** Tree layout: root on the left, children stacked per depth (tidy enough for families up to ~40 members). */
function layout(nodes: Node[]) {
  const children = new Map<string, Node[]>();
  nodes.forEach((n) => {
    if (n.parent) children.set(n.parent, [...(children.get(n.parent) ?? []), n]);
  });
  const root = nodes.find((n) => !n.parent) ?? nodes[0];
  const pos = new Map<string, { x: number; y: number }>();
  let row = 0;
  const place = (n: Node, depth: number): number => {
    const kids = (children.get(n.id) ?? []).sort((a, b) => (b.similarity ?? 0) - (a.similarity ?? 0));
    if (!kids.length) {
      const y = row++;
      pos.set(n.id, { x: depth, y });
      return y;
    }
    const ys = kids.map((k) => place(k, depth + 1));
    const y = (ys[0] + ys[ys.length - 1]) / 2;
    pos.set(n.id, { x: depth, y });
    return y;
  };
  if (root) place(root, 0);
  nodes.forEach((n) => {
    if (!pos.has(n.id)) pos.set(n.id, { x: 0, y: row++ });
  });
  const maxX = Math.max(0, ...Array.from(pos.values()).map((p) => p.x));
  return { pos, width: (maxX + 1) * (NODE_W + GAP_X), height: Math.max(1, row) * (NODE_H + GAP_Y) };
}

const EVAL = new Set(["val", "test"]);

export function LineageGraph({ nodes }: { nodes: Node[] }) {
  const { pos, width, height } = useMemo(() => layout(nodes), [nodes]);
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const px = (id: string) => {
    const p = pos.get(id)!;
    return { x: p.x * (NODE_W + GAP_X), y: p.y * (NODE_H + GAP_Y) };
  };
  return (
    <div className="overflow-auto rounded-lg border hairline bg-surface-2/40 p-4">
      <div className="relative" style={{ width, height }}>
        <svg width={width} height={height} className="absolute inset-0" aria-hidden>
          {nodes
            .filter((n) => n.parent && byId.has(n.parent))
            .map((n) => {
              const a = px(n.parent!);
              const b = px(n.id);
              const x1 = a.x + NODE_W;
              const y1 = a.y + 50;
              const x2 = b.x;
              const y2 = b.y + 50;
              const crosses = byId.get(n.parent!)!.sample.split !== n.sample.split && (EVAL.has(n.sample.split) || EVAL.has(byId.get(n.parent!)!.sample.split));
              return (
                <g key={n.id}>
                  <path
                    d={`M${x1},${y1} C${x1 + GAP_X / 2},${y1} ${x2 - GAP_X / 2},${y2} ${x2},${y2}`}
                    fill="none"
                    stroke={crosses ? "var(--bad)" : "var(--border-strong)"}
                    strokeWidth={crosses ? 2.5 : 1.5}
                    strokeDasharray={n.relation === "exact" ? undefined : "5 4"}
                  />
                </g>
              );
            })}
        </svg>
        {nodes.map((n) => {
          const p = px(n.id);
          const parent = n.parent ? byId.get(n.parent) : null;
          const crosses = parent && parent.sample.split !== n.sample.split && (EVAL.has(n.sample.split) || EVAL.has(parent.sample.split));
          return (
            <div key={n.id} className="absolute" style={{ left: p.x, top: p.y, width: NODE_W }}>
              {n.relation && (
                <div className="absolute -left-[68px] top-[58px] w-[64px] text-center text-[10px] leading-tight text-muted">
                  <div className={crosses ? "font-semibold text-bad" : undefined}>{humanize(n.relation)}</div>
                  {n.similarity !== null && <div className="tabular">cos {fixed(n.similarity, 3)}</div>}
                </div>
              )}
              <div className={crosses ? "rounded-xl ring-2 ring-bad ring-offset-2 ring-offset-surface" : !n.parent ? "rounded-xl ring-2 ring-accent ring-offset-2 ring-offset-surface" : undefined}>
                <SampleThumb s={n.sample} size={NODE_W} caption={false} />
              </div>
              <div className="mt-1 flex items-center gap-1 text-[11px]">
                <SplitTag split={n.sample.split} />
                <span className="truncate font-medium" title={n.sample.label}>
                  {n.sample.label}
                </span>
              </div>
              {!n.parent && <div className="text-[10.5px] text-accent">source-like (largest)</div>}
              {crosses && <div className="text-[10.5px] font-medium text-bad">⚠ crosses splits</div>}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function LineageLegend({ legend }: { legend: Record<string, string> }) {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-muted">
      <span className="flex items-center gap-1.5">
        <svg width="28" height="6" aria-hidden>
          <line x1="0" y1="3" x2="28" y2="3" stroke="var(--border-strong)" strokeWidth="1.5" />
        </svg>
        identical file <KindTag kind="measured" />
      </span>
      <span className="flex items-center gap-1.5">
        <svg width="28" height="6" aria-hidden>
          <line x1="0" y1="3" x2="28" y2="3" stroke="var(--border-strong)" strokeWidth="1.5" strokeDasharray="5 4" />
        </svg>
        transform / near duplicate <KindTag kind="heuristic" />
      </span>
      <span className="flex items-center gap-1.5">
        <svg width="28" height="6" aria-hidden>
          <line x1="0" y1="3" x2="28" y2="3" stroke="var(--bad)" strokeWidth="2.5" />
        </svg>
        crosses train ↔ evaluation (possible leakage)
      </span>
      {Object.entries(legend)
        .filter(([k]) => k !== "exact")
        .map(([k, v]) => (
          <Badge key={k} title={v}>
            {humanize(k)}
          </Badge>
        ))}
    </div>
  );
}
