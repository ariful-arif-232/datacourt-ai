"use client";

import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { cx } from "@/components/ui";

/* Categorical slots in fixed order (validated palette); never cycled — extra series fold into "Other". */
export const SERIES = Array.from({ length: 8 }, (_, i) => `var(--series-${i + 1})`);
export const OTHER = "var(--series-other)";

export function colorFor(key: string, order: string[]): string {
  const i = order.indexOf(key);
  return i >= 0 && i < SERIES.length ? SERIES[i] : OTHER;
}

/** Resolve a CSS variable to a concrete color (needed for canvas). */
function resolveColor(el: HTMLElement | null, value: string): string {
  if (!el || !value.startsWith("var(")) return value;
  const name = value.slice(4, -1);
  return getComputedStyle(el).getPropertyValue(name).trim() || "#888";
}

function Tooltip({ x, y, children }: { x: number; y: number; children: ReactNode }) {
  return (
    <div
      role="tooltip"
      className="pointer-events-none absolute z-30 max-w-64 rounded-md border border-border bg-surface px-2.5 py-1.5 text-xs text-ink shadow-lg"
      style={{ left: x + 12, top: y + 12 }}
    >
      {children}
    </div>
  );
}

export function Legend({ items }: { items: { key: string; label: string; color: string; count?: number }[] }) {
  return (
    <ul className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted" aria-label="Legend">
      {items.map((it) => (
        <li key={it.key} className="flex items-center gap-1.5">
          <span className="size-2.5 rounded-[3px]" style={{ background: it.color }} aria-hidden />
          <span className="text-ink">{it.label}</span>
          {it.count !== undefined && <span className="tabular">{it.count.toLocaleString()}</span>}
        </li>
      ))}
    </ul>
  );
}

/* ---------------------------------------------------------------- bar list */

export function BarList({ data, format = (v) => v.toLocaleString(), max, color = "var(--series-1)", onSelect }: {
  data: { label: string; value: number; hint?: string; color?: string }[];
  format?: (v: number) => string;
  max?: number;
  color?: string;
  onSelect?: (label: string) => void;
}) {
  const m = max ?? Math.max(1, ...data.map((d) => d.value));
  return (
    <ul className="space-y-1.5">
      {data.map((d) => (
        <li key={d.label}>
          <button
            type="button"
            disabled={!onSelect}
            onClick={() => onSelect?.(d.label)}
            className={cx("grid w-full grid-cols-[minmax(80px,30%)_1fr_auto] items-center gap-3 rounded px-1 py-0.5 text-left text-[13px]", onSelect && "hover:bg-surface-2")}
            title={d.hint ?? `${d.label}: ${format(d.value)}`}
          >
            <span className="truncate">{d.label}</span>
            <span className="h-2.5 rounded-full bg-surface-2">
              <span className="block h-full rounded-full" style={{ width: `${(d.value / m) * 100}%`, background: d.color ?? color }} />
            </span>
            <span className="tabular text-muted">{format(d.value)}</span>
          </button>
        </li>
      ))}
    </ul>
  );
}

/* ---------------------------------------------------------------- histogram */

export function Histogram({ counts, edges, height = 120, xLabel, format = (v: number) => v.toFixed(0) }: { counts: number[]; edges: number[]; height?: number; xLabel?: string; format?: (v: number) => string }) {
  const [hover, setHover] = useState<{ i: number; x: number; y: number } | null>(null);
  const max = Math.max(1, ...counts);
  const W = 100 / Math.max(1, counts.length);
  return (
    <div className="relative" onMouseLeave={() => setHover(null)}>
      <svg viewBox={`0 0 100 ${height}`} preserveAspectRatio="none" className="w-full" style={{ height }} role="img" aria-label={`Histogram${xLabel ? ` of ${xLabel}` : ""}`}>
        <line x1="0" x2="100" y1={height - 0.5} y2={height - 0.5} stroke="var(--axis)" strokeWidth="1" vectorEffect="non-scaling-stroke" />
        {counts.map((c, i) => {
          const h = (c / max) * (height - 6);
          return (
            <g key={i}>
              <rect
                x={i * W + 0.3}
                y={height - h}
                width={Math.max(0.2, W - 0.6)}
                height={h}
                rx="0.8"
                fill={hover?.i === i ? "var(--seq-500)" : "var(--seq-300)"}
              />
              <rect
                x={i * W}
                y={0}
                width={W}
                height={height}
                fill="transparent"
                onMouseMove={(e) => {
                  const r = (e.currentTarget.ownerSVGElement as SVGSVGElement).getBoundingClientRect();
                  setHover({ i, x: e.clientX - r.left, y: e.clientY - r.top });
                }}
              />
            </g>
          );
        })}
      </svg>
      {edges.length > 1 && (
        <div className="mt-1 flex justify-between text-[11px] text-subtle tabular">
          <span>{format(edges[0])}</span>
          {xLabel && <span>{xLabel}</span>}
          <span>{format(edges[edges.length - 1])}</span>
        </div>
      )}
      {hover && (
        <Tooltip x={hover.x} y={hover.y}>
          <div className="tabular">
            {format(edges[hover.i])} – {format(edges[hover.i + 1])}
          </div>
          <div className="font-semibold tabular">{counts[hover.i].toLocaleString()} samples</div>
        </Tooltip>
      )}
    </div>
  );
}

/* ---------------------------------------------------------------- stacked bars */

export function StackedBars({ rows, keys, keyLabels }: { rows: { label: string; values: Record<string, number> }[]; keys: string[]; keyLabels?: Record<string, string> }) {
  const [hover, setHover] = useState<{ row: string; key: string; x: number; y: number } | null>(null);
  const max = Math.max(1, ...rows.map((r) => keys.reduce((a, k) => a + (r.values[k] || 0), 0)));
  const ref = useRef<HTMLDivElement>(null);
  return (
    <div ref={ref} className="relative space-y-3" onMouseLeave={() => setHover(null)}>
      <Legend items={keys.map((k, i) => ({ key: k, label: keyLabels?.[k] ?? k, color: SERIES[i] ?? OTHER }))} />
      <ul className="space-y-1.5">
        {rows.map((r) => {
          const total = keys.reduce((a, k) => a + (r.values[k] || 0), 0);
          return (
            <li key={r.label} className="grid grid-cols-[minmax(80px,28%)_1fr_auto] items-center gap-3 text-[13px]">
              <span className="truncate">{r.label}</span>
              <span className="flex h-3 gap-[2px]" style={{ width: `${(total / max) * 100}%` }}>
                {keys.map((k, i) =>
                  r.values[k] ? (
                    <span
                      key={k}
                      className="h-full first:rounded-l-[4px] last:rounded-r-[4px]"
                      style={{ flexGrow: r.values[k], background: SERIES[i] ?? OTHER }}
                      onMouseMove={(e) => {
                        const b = ref.current!.getBoundingClientRect();
                        setHover({ row: r.label, key: k, x: e.clientX - b.left, y: e.clientY - b.top });
                      }}
                    />
                  ) : null,
                )}
              </span>
              <span className="tabular text-muted">{total.toLocaleString()}</span>
            </li>
          );
        })}
      </ul>
      {hover && (
        <Tooltip x={hover.x} y={hover.y}>
          <div className="text-muted">{hover.row}</div>
          <div>
            <span className="font-semibold tabular">{(rows.find((r) => r.label === hover.row)?.values[hover.key] ?? 0).toLocaleString()}</span> in {keyLabels?.[hover.key] ?? hover.key}
          </div>
        </Tooltip>
      )}
    </div>
  );
}

/* ---------------------------------------------------------------- scatter (canvas) */

export type Point = { id: string; x: number; y: number; group: string; size?: number; muted?: boolean; meta?: ReactNode };

export function ScatterCanvas({ points, groups, height = 440, onPick, highlight, xLabel, yLabel, domain }: {
  points: Point[];
  groups: string[];
  height?: number;
  onPick?: (p: Point) => void;
  highlight?: string | null;
  xLabel?: string;
  yLabel?: string;
  domain?: { x: [number, number]; y: [number, number] };
}) {
  const wrap = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const [w, setW] = useState(600);
  const [hover, setHover] = useState<{ p: Point; x: number; y: number } | null>(null);
  const pad = 14;
  const dom = useMemo(() => {
    if (domain) return domain;
    const xs = points.map((p) => p.x);
    const ys = points.map((p) => p.y);
    return { x: [Math.min(...xs, -1), Math.max(...xs, 1)] as [number, number], y: [Math.min(...ys, -1), Math.max(...ys, 1)] as [number, number] };
  }, [points, domain]);
  const sx = (x: number) => pad + ((x - dom.x[0]) / (dom.x[1] - dom.x[0] || 1)) * (w - 2 * pad);
  const sy = (y: number) => height - pad - ((y - dom.y[0]) / (dom.y[1] - dom.y[0] || 1)) * (height - 2 * pad);

  useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setW(el.clientWidth));
    ro.observe(el);
    setW(el.clientWidth);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    const c = canvas.current;
    if (!c) return;
    const dpr = window.devicePixelRatio || 1;
    c.width = w * dpr;
    c.height = height * dpr;
    const ctx = c.getContext("2d")!;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, w, height);
    const colors = groups.map((g) => resolveColor(c, colorFor(g, groups)));
    const other = resolveColor(c, OTHER);
    const surface = resolveColor(c, "var(--surface)");
    const sorted = [...points].sort((a, b) => Number(!!b.muted) - Number(!!a.muted) || (highlight ? Number(a.group === highlight) - Number(b.group === highlight) : 0));
    for (const p of sorted) {
      const gi = groups.indexOf(p.group);
      const dim = p.muted || (highlight && p.group !== highlight);
      ctx.globalAlpha = dim ? 0.18 : 0.85;
      ctx.beginPath();
      ctx.arc(sx(p.x), sy(p.y), p.size ?? 3, 0, Math.PI * 2);
      ctx.fillStyle = gi >= 0 && gi < 8 ? colors[gi] : other;
      ctx.fill();
      if (!dim) {
        ctx.lineWidth = 0.75;
        ctx.strokeStyle = surface;
        ctx.stroke();
      }
    }
    ctx.globalAlpha = 1;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [points, groups, w, height, highlight, dom]);

  const nearest = (mx: number, my: number): Point | null => {
    let best: Point | null = null;
    let bd = 100;
    for (const p of points) {
      const d = (sx(p.x) - mx) ** 2 + (sy(p.y) - my) ** 2;
      if (d < bd) {
        bd = d;
        best = p;
      }
    }
    return best;
  };

  return (
    <div ref={wrap} className="relative w-full" onMouseLeave={() => setHover(null)}>
      <canvas
        ref={canvas}
        style={{ width: "100%", height, cursor: onPick ? "pointer" : "default" }}
        className="rounded-lg bg-surface-2/50"
        role="img"
        aria-label={`Scatter plot of ${points.length} points${xLabel ? `, x: ${xLabel}` : ""}${yLabel ? `, y: ${yLabel}` : ""}`}
        onMouseMove={(e) => {
          const r = e.currentTarget.getBoundingClientRect();
          const p = nearest(e.clientX - r.left, e.clientY - r.top);
          setHover(p ? { p, x: e.clientX - r.left, y: e.clientY - r.top } : null);
        }}
        onClick={() => hover && onPick?.(hover.p)}
      />
      {xLabel && <div className="mt-1 text-center text-[11px] text-subtle">{xLabel}</div>}
      {yLabel && <div className="absolute left-1 top-1 text-[11px] text-subtle">{yLabel}</div>}
      {hover && <Tooltip x={hover.x} y={hover.y}>{hover.p.meta ?? hover.p.group}</Tooltip>}
    </div>
  );
}

/* ---------------------------------------------------------------- small line chart */

export function LineChart({ series, height = 140, yDomain = [0, 1], xLabels, format = (v: number) => v.toFixed(2) }: {
  series: { key: string; label: string; values: number[]; color?: string }[];
  height?: number;
  yDomain?: [number, number];
  xLabels?: string[];
  format?: (v: number) => string;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const n = Math.max(0, ...series.map((s) => s.values.length));
  const W = 300;
  const x = (i: number) => (n <= 1 ? W / 2 : 8 + (i / (n - 1)) * (W - 16));
  const y = (v: number) => height - 6 - ((v - yDomain[0]) / (yDomain[1] - yDomain[0] || 1)) * (height - 12);
  return (
    <div className="relative">
      {series.length > 1 && <Legend items={series.map((s, i) => ({ key: s.key, label: s.label, color: s.color ?? SERIES[i] }))} />}
      <svg
        viewBox={`0 0 ${W} ${height}`}
        className="mt-2 w-full"
        style={{ height }}
        role="img"
        aria-label={series.map((s) => s.label).join(", ")}
        onMouseLeave={() => setHover(null)}
        onMouseMove={(e) => {
          const r = e.currentTarget.getBoundingClientRect();
          const rel = ((e.clientX - r.left) / r.width) * W;
          setHover(Math.max(0, Math.min(n - 1, Math.round(((rel - 8) / (W - 16)) * (n - 1)))));
        }}
      >
        {[0, 0.5, 1].map((t) => (
          <line key={t} x1="0" x2={W} y1={y(yDomain[0] + t * (yDomain[1] - yDomain[0]))} y2={y(yDomain[0] + t * (yDomain[1] - yDomain[0]))} stroke="var(--grid)" strokeWidth="1" />
        ))}
        {series.map((s, si) => (
          <polyline key={s.key} fill="none" stroke={s.color ?? SERIES[si]} strokeWidth="2" strokeLinejoin="round" points={s.values.map((v, i) => `${x(i)},${y(v)}`).join(" ")} />
        ))}
        {hover !== null && <line x1={x(hover)} x2={x(hover)} y1="0" y2={height} stroke="var(--axis)" strokeDasharray="3 3" />}
        {hover !== null &&
          series.map((s, si) => (s.values[hover] !== undefined ? <circle key={s.key} cx={x(hover)} cy={y(s.values[hover])} r="4" fill={s.color ?? SERIES[si]} stroke="var(--surface)" strokeWidth="2" /> : null))}
      </svg>
      {hover !== null && (
        <div className="absolute right-0 top-0 rounded-md border border-border bg-surface px-2 py-1 text-xs shadow">
          <div className="text-muted">{xLabels?.[hover] ?? `#${hover + 1}`}</div>
          {series.map((s) => (
            <div key={s.key} className="tabular">
              {series.length > 1 && <span className="text-muted">{s.label}: </span>}
              {s.values[hover] !== undefined ? format(s.values[hover]) : "—"}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/* ---------------------------------------------------------------- confusion matrix */

export function ConfusionMatrix({ matrix, labels }: { matrix: number[][]; labels: string[] }) {
  const max = Math.max(1, ...matrix.flat());
  const [hover, setHover] = useState<[number, number] | null>(null);
  return (
    <div className="overflow-x-auto">
      <table className="text-[11px] tabular" aria-label="Confusion matrix: rows are true labels, columns are predictions">
        <thead>
          <tr>
            <th className="p-1 text-left font-normal text-subtle">true ↓ / pred →</th>
            {labels.map((l) => (
              <th key={l} className="max-w-16 truncate p-1 font-normal text-subtle" title={l}>
                {l}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {matrix.map((row, i) => (
            <tr key={i}>
              <th className="max-w-24 truncate p-1 text-left font-normal text-muted" title={labels[i]}>
                {labels[i]}
              </th>
              {row.map((v, j) => {
                const t = v / max;
                const bg = i === j ? `color-mix(in srgb, var(--seq-500) ${Math.round(15 + 70 * t)}%, var(--surface))` : v ? `color-mix(in srgb, var(--series-2) ${Math.round(12 + 60 * t)}%, var(--surface))` : "var(--surface-2)";
                return (
                  <td
                    key={j}
                    className="h-7 w-10 text-center"
                    style={{ background: bg, outline: hover && hover[0] === i && hover[1] === j ? "2px solid var(--ink)" : undefined }}
                    title={`true ${labels[i]} → predicted ${labels[j]}: ${v}`}
                    onMouseEnter={() => setHover([i, j])}
                    onMouseLeave={() => setHover(null)}
                  >
                    {v || ""}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ---------------------------------------------------------------- dna strip */

export function DnaStrip({ bands, height = 22, label }: { bands: number[]; height?: number; label?: string }) {
  return (
    <div className="flex gap-[2px]" style={{ height }} role="img" aria-label={label ?? "Dataset DNA strip"}>
      {bands.map((b, i) => (
        <span key={i} className="flex-1 rounded-[3px]" style={{ background: `color-mix(in srgb, var(--accent) ${Math.round(12 + 88 * b)}%, var(--surface-2))` }} title={`band ${i + 1}: ${b.toFixed(2)}`} />
      ))}
    </div>
  );
}

/* ---------------------------------------------------------------- meter */

export function Meter({ value, max = 1, tone = "accent", label }: { value: number; max?: number; tone?: "accent" | "bad" | "warn" | "ok"; label?: string }) {
  const v = Math.max(0, Math.min(1, value / (max || 1)));
  return (
    <div className="flex items-center gap-2" aria-label={label}>
      <div className="h-1.5 flex-1 rounded-full bg-surface-3">
        <div className="h-full rounded-full" style={{ width: `${v * 100}%`, background: `var(--${tone === "accent" ? "accent" : tone})` }} />
      </div>
    </div>
  );
}
