"use client";

import { useMemo, useState } from "react";
import { ClipboardList } from "lucide-react";
import { RequireAudit } from "@/components/audit-progress";
import { colorFor, Legend, type Point, ScatterCanvas } from "@/components/charts";
import { SampleModal } from "@/components/sample-detail";
import { Badge, Button, Card, EmptyState, Loading, Notice, PageHeader, SampleThumb, Select, StatusBadge, Tabs } from "@/components/ui";
import { api } from "@/lib/api";
import { useDataset } from "@/lib/dataset";
import { fixed, humanize, pct } from "@/lib/format";
import { useVersionApi } from "@/lib/hooks";
import { useSession } from "@/lib/session";

const PRIORITY_TONE: Record<string, "bad" | "warn" | "neutral"> = { high: "bad", medium: "warn", low: "neutral" };

export default function CoveragePage() {
  const { versionId } = useDataset();
  const { canWrite } = useSession();
  const { data, mutate } = useVersionApi<any>("/coverage");
  const [colorBy, setColorBy] = useState<"label" | "split" | "sparse">("label");
  const [focus, setFocus] = useState<string>("");
  const [tab, setTab] = useState<"planner" | "regions" | "tasks">("planner");
  const [open, setOpen] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const groups = useMemo(() => {
    if (!data) return [];
    if (colorBy === "label") return data.classes as string[];
    if (colorBy === "split") return Array.from(new Set((data.points as any[]).map((p) => p.split))).sort();
    return ["sparse (bottom 10%)", "typical"];
  }, [data, colorBy]);
  const points: Point[] = useMemo(
    () =>
      (data?.points ?? []).map((p: any) => ({
        id: p.id,
        x: p.x,
        y: p.y,
        group: colorBy === "label" ? p.label : colorBy === "split" ? p.split : p.density_pct < 0.1 ? "sparse (bottom 10%)" : "typical",
        muted: !!focus && p.label !== focus,
        meta: (
          <span>
            <b>{p.label}</b> · {p.split}
            <br />
            region {p.cluster} · density pct {pct(p.density_pct)}
          </span>
        ),
      })),
    [data, colorBy, focus],
  );

  const createTask = async (gapId: string) => {
    setMsg(null);
    try {
      await api(`/versions/${versionId}/collection-tasks`, { json: { gap_id: gapId } });
      setMsg("Collection task created.");
      mutate();
    } catch (e: any) {
      setMsg(e.message);
    }
  };
  const setStatus = async (taskId: string, status: string) => {
    await api(`/collection-tasks/${taskId}`, { method: "PATCH", json: { status } });
    mutate();
  };

  return (
    <RequireAudit>
      <PageHeader
        title="Coverage map & Active Collection Planner"
        description="Where the dataset is dense, sparse, mixed or missing — and what to collect next. DataCourt recommends collection targets; it never fabricates data."
      />
      {!data ? (
        <Loading />
      ) : (
        <div className="space-y-5">
          <Card
            title="Coverage map"
            subtitle={`2-D projection (${data.projection}) of the embedding space${data.sampled_every > 1 ? `, every ${data.sampled_every}th sample shown` : ""}. Click a point to inspect it.`}
            actions={
              <>
                <Select label="Colour by" value={colorBy} onChange={(v) => setColorBy(v as any)} options={[{ value: "label", label: "Colour: class" }, { value: "split", label: "Colour: split" }, { value: "sparse", label: "Colour: sparsity" }]} />
                <Select label="Focus class" value={focus} onChange={setFocus} options={[{ value: "", label: "All classes" }, ...data.classes.map((c: string) => ({ value: c, label: `Focus: ${c}` }))]} />
              </>
            }
          >
            <div className="mb-3">
              <Legend items={groups.slice(0, 8).map((g) => ({ key: g, label: g, color: colorFor(g, groups) }))} />
              {groups.length > 8 && <p className="mt-1 text-xs text-subtle">{groups.length - 8} more classes are shown in grey (“other”); use “Focus class” to isolate one.</p>}
            </div>
            <ScatterCanvas points={points} groups={groups} onPick={(p) => setOpen(p.id)} />
            <p className="mt-2 text-xs text-subtle">
              Distances in a 2-D projection are approximate; regions and density are computed in the full embedding space.
            </p>
          </Card>
          <Tabs
            value={tab}
            onChange={setTab}
            tabs={[
              { value: "planner", label: "Collection planner", count: data.gaps.length },
              { value: "regions", label: "Regions", count: data.clusters.length },
              { value: "tasks", label: "Collection tasks", count: data.collection_tasks.length },
            ]}
          />
          {msg && <Notice>{msg}</Notice>}
          {tab === "planner" &&
            (data.gaps.length === 0 ? (
              <EmptyState title="No coverage gaps above thresholds" />
            ) : (
              <ul className="grid gap-4 lg:grid-cols-2">
                {data.gaps.map((g: any) => (
                  <li key={g.id} className="card p-4">
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge tone={PRIORITY_TONE[g.priority]}>{g.priority} priority</Badge>
                      <Badge>{humanize(g.kind)}</Badge>
                      <span className="text-xs text-subtle tabular">score {fixed(g.priority_score, 2)}</span>
                    </div>
                    <h3 className="mt-2 font-display text-lg leading-snug">{g.title}</h3>
                    <p className="mt-1 text-sm text-muted">{g.description}</p>
                    <div className="mt-3 flex flex-wrap items-center gap-3 text-sm">
                      <span>
                        Suggested: <b className="tabular">{g.suggested_quantity}</b> images
                      </span>
                      {g.class && <span className="text-muted">class: {g.class}</span>}
                      {g.condition?.description && <span className="text-muted">condition: {g.condition.description}</span>}
                    </div>
                    {g.anchors.length > 0 && (
                      <div className="mt-3">
                        <div className="mb-1 text-xs text-subtle">Nearest existing examples (anchors)</div>
                        <ul className="flex gap-2 overflow-x-auto">
                          {g.anchors.map((s: any) => (
                            <li key={s.id}>
                              <button onClick={() => setOpen(s.id)} aria-label={`Open ${s.name}`}>
                                <SampleThumb s={s} size={64} caption={false} />
                              </button>
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}
                    {canWrite && (
                      <Button size="sm" className="mt-3" onClick={() => createTask(g.id)}>
                        <ClipboardList className="size-4" aria-hidden /> Create collection task
                      </Button>
                    )}
                  </li>
                ))}
              </ul>
            ))}
          {tab === "regions" && (
            <div className="card overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-left text-xs text-subtle">
                  <tr className="border-b hairline">
                    <th className="px-3 py-2 font-medium">Region</th>
                    <th className="px-3 py-2 font-medium">Size</th>
                    <th className="px-3 py-2 font-medium">Dominant class</th>
                    <th className="px-3 py-2 font-medium">Purity</th>
                    <th className="px-3 py-2 font-medium">Density</th>
                    <th className="px-3 py-2 font-medium">Splits</th>
                    <th className="px-3 py-2 font-medium">Background · brightness</th>
                  </tr>
                </thead>
                <tbody>
                  {data.clusters.map((c: any) => (
                    <tr key={c.index} className="border-b hairline last:border-0">
                      <td className="px-3 py-2">
                        {c.index} {c.is_sparse && <Badge tone="warn">sparse</Badge>}
                      </td>
                      <td className="px-3 py-2 tabular">{c.size}</td>
                      <td className="px-3 py-2">{c.dominant_class}</td>
                      <td className="px-3 py-2 tabular">{pct(c.purity)}</td>
                      <td className="px-3 py-2 tabular">{fixed(c.density, 3)}</td>
                      <td className="px-3 py-2 text-xs text-muted">{Object.entries(c.splits).map(([k, v]) => `${k} ${v}`).join(" · ")}</td>
                      <td className="px-3 py-2 text-xs text-muted">
                        {c.attributes.dominant_border_color} · {fixed(c.attributes.mean_brightness, 0)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {tab === "tasks" &&
            (data.collection_tasks.length === 0 ? (
              <EmptyState title="No collection tasks yet">Turn a recommendation into a task to track new data collection across versions.</EmptyState>
            ) : (
              <ul className="space-y-3">
                {data.collection_tasks.map((t: any) => (
                  <li key={t.id} className="card flex flex-wrap items-center gap-3 p-4 text-sm">
                    <StatusBadge status={t.status} />
                    <Badge tone={PRIORITY_TONE[t.priority]}>{t.priority}</Badge>
                    <span className="font-medium">{t.target_class}</span>
                    <span className="text-muted">{t.target_condition}</span>
                    <span className="tabular text-muted">{t.quantity} images</span>
                    <div className="flex-1" />
                    {canWrite && (
                      <Select label="Task status" value={t.status} onChange={(v) => setStatus(t.id, v)} options={["open", "in_progress", "done", "dismissed"].map((s) => ({ value: s, label: humanize(s) }))} />
                    )}
                  </li>
                ))}
              </ul>
            ))}
          <p className="text-xs text-subtle">{data.disclaimer}</p>
        </div>
      )}
      <SampleModal sampleId={open} versionId={versionId} onClose={() => setOpen(null)} />
    </RequireAudit>
  );
}
