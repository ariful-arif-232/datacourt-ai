"use client";

import Link from "next/link";
import {
  AlertTriangle,
  Ban,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CircleDashed,
  CircleHelp,
  Gem,
  Info,
  Loader2,
  Scale,
  ShieldAlert,
  XCircle,
} from "lucide-react";
import { type ButtonHTMLAttributes, type ReactNode, useEffect, useId, useRef, useState } from "react";
import { GLOSSARY } from "@/lib/glossary";
import { humanize } from "@/lib/format";

export function cx(...c: (string | false | null | undefined)[]) {
  return c.filter(Boolean).join(" ");
}

/* ---------------------------------------------------------------- buttons */

type BtnProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost" | "danger";
  size?: "sm" | "md";
  loading?: boolean;
  href?: string;
  target?: string;
  rel?: string;
};

export function Button({ variant = "secondary", size = "md", loading, className, children, href, target, rel, ...rest }: BtnProps) {
  const cls = cx(
    "inline-flex items-center justify-center gap-1.5 rounded-lg font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed whitespace-nowrap",
    size === "sm" ? "h-8 px-2.5 text-[13px]" : "h-9 px-3.5 text-sm",
    variant === "primary" && "bg-accent text-accent-ink hover:opacity-90",
    variant === "secondary" && "border border-border bg-surface hover:bg-surface-2 text-ink",
    variant === "ghost" && "text-muted hover:bg-surface-2 hover:text-ink",
    variant === "danger" && "border border-bad/40 bg-bad-soft text-bad hover:bg-bad hover:text-white",
    className,
  );
  if (href && /^https?:/.test(href)) {
    return (
      <a href={href} className={cls} target={target} rel={rel ?? "noopener noreferrer"}>
        {children}
      </a>
    );
  }
  if (href) {
    return (
      <Link href={href} className={cls}>
        {children}
      </Link>
    );
  }
  return (
    <button className={cls} disabled={loading || rest.disabled} {...rest}>
      {loading && <Loader2 className="size-4 animate-spin" aria-hidden />}
      {children}
    </button>
  );
}

/* ---------------------------------------------------------------- badges */

export type Tone = "neutral" | "ok" | "warn" | "bad" | "info" | "accent";

const toneCls: Record<Tone, string> = {
  neutral: "bg-surface-2 text-muted border-border",
  ok: "bg-ok-soft text-ok border-ok/25",
  warn: "bg-warn-soft text-warn border-warn/25",
  bad: "bg-bad-soft text-bad border-bad/25",
  info: "bg-info-soft text-info border-info/25",
  accent: "bg-accent-soft text-accent border-accent/25",
};

export function Badge({ tone = "neutral", children, icon, className, title }: { tone?: Tone; children: ReactNode; icon?: ReactNode; className?: string; title?: string }) {
  return (
    <span title={title} className={cx("inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[11.5px] font-medium leading-none whitespace-nowrap", toneCls[tone], className)}>
      {icon}
      {children}
    </span>
  );
}

const VERDICT: Record<string, { tone: Tone; icon: ReactNode }> = {
  KEEP: { tone: "ok", icon: <CheckCircle2 className="size-3" aria-hidden /> },
  REVIEW: { tone: "info", icon: <CircleDashed className="size-3" aria-hidden /> },
  STRONG_REVIEW: { tone: "warn", icon: <AlertTriangle className="size-3" aria-hidden /> },
  POSSIBLE_RELABEL: { tone: "bad", icon: <Scale className="size-3" aria-hidden /> },
  POSSIBLE_REMOVE: { tone: "bad", icon: <XCircle className="size-3" aria-hidden /> },
  LIKELY_RARE: { tone: "accent", icon: <Gem className="size-3" aria-hidden /> },
  LEAKAGE_ACTION_NEEDED: { tone: "bad", icon: <ShieldAlert className="size-3" aria-hidden /> },
  UNCERTAIN: { tone: "neutral", icon: <CircleHelp className="size-3" aria-hidden /> },
};

export function VerdictBadge({ verdict }: { verdict: string }) {
  const v = VERDICT[verdict] ?? VERDICT.UNCERTAIN;
  return (
    <Badge tone={v.tone} icon={v.icon}>
      {humanize(verdict)}
    </Badge>
  );
}

export function LevelBadge({ level }: { level: string | null | undefined }) {
  if (!level) return <Badge>—</Badge>;
  const tone: Tone = level === "LOW" ? "ok" : level === "MODERATE" ? "warn" : "bad";
  const icon = level === "LOW" ? <CheckCircle2 className="size-3" aria-hidden /> : <AlertTriangle className="size-3" aria-hidden />;
  return (
    <Badge tone={tone} icon={icon}>
      {level}
    </Badge>
  );
}

export function PreflightBadge({ status }: { status: string | null | undefined }) {
  if (!status) return <Badge>—</Badge>;
  const map: Record<string, [Tone, ReactNode]> = {
    READY: ["ok", <CheckCircle2 key="i" className="size-3" aria-hidden />],
    READY_WITH_WARNINGS: ["warn", <AlertTriangle key="i" className="size-3" aria-hidden />],
    BLOCKED: ["bad", <Ban key="i" className="size-3" aria-hidden />],
  };
  const [tone, icon] = map[status] ?? ["neutral", null];
  return (
    <Badge tone={tone} icon={icon}>
      {humanize(status)}
    </Badge>
  );
}

export function StatusBadge({ status }: { status: string }) {
  const tone: Tone =
    status === "completed" || status === "ready" || status === "resolved" || status === "decided" || status === "pass"
      ? "ok"
      : status === "failed" || status === "block" || status === "disputed"
        ? "bad"
        : status === "running" || status === "ingesting" || status === "queued" || status === "uploaded"
          ? "info"
          : status === "warn" || status === "skipped" || status === "cancelled"
            ? "warn"
            : "neutral";
  const icon =
    status === "running" || status === "ingesting" ? <Loader2 className="size-3 animate-spin" aria-hidden /> : null;
  return (
    <Badge tone={tone} icon={icon}>
      {humanize(status)}
    </Badge>
  );
}

export const KIND_LABEL: Record<string, string> = {
  measured: "Measured",
  heuristic: "Heuristic",
  model_prediction: "Model prediction",
  model_estimate: "Model estimate",
  llm: "LLM explanation",
  human: "Human decision",
};

const KIND_VAR: Record<string, string> = {
  measured: "var(--k-measured)",
  heuristic: "var(--k-heuristic)",
  model_prediction: "var(--k-prediction)",
  model_estimate: "var(--k-estimate)",
  llm: "var(--k-llm)",
  human: "var(--k-human)",
};

/** Distinguishes measured fact / heuristic / model prediction / model estimate / LLM / human decision. */
export function KindTag({ kind }: { kind: string }) {
  const color = KIND_VAR[kind] ?? "var(--subtle)";
  return (
    <span
      className="inline-flex items-center gap-1 rounded-full border px-1.5 py-[1px] text-[10.5px] font-medium uppercase tracking-wide"
      style={{ color, borderColor: `color-mix(in srgb, ${color} 35%, transparent)` }}
    >
      <span className="size-1.5 rounded-full" style={{ background: color }} aria-hidden />
      {KIND_LABEL[kind] ?? kind}
    </span>
  );
}

export function SplitTag({ split }: { split: string }) {
  const tone: Tone = split === "train" ? "neutral" : split === "val" ? "info" : split === "test" ? "accent" : "neutral";
  return <Badge tone={tone}>{split}</Badge>;
}

/* ---------------------------------------------------------------- layout */

export function Card({ children, className, title, actions, info, subtitle }: { children: ReactNode; className?: string; title?: ReactNode; actions?: ReactNode; info?: string; subtitle?: ReactNode }) {
  return (
    <section className={cx("card", className)}>
      {(title || actions) && (
        <header className="flex items-start justify-between gap-3 border-b hairline px-4 py-3">
          <div className="min-w-0">
            <h3 className="flex items-center gap-1.5 text-sm font-semibold">
              {title}
              {info && <InfoTip term={info} />}
            </h3>
            {subtitle && <p className="mt-0.5 text-xs text-muted">{subtitle}</p>}
          </div>
          {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

export function PageHeader({ title, description, actions, eyebrow }: { title: ReactNode; description?: ReactNode; actions?: ReactNode; eyebrow?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
      <div className="min-w-0">
        {eyebrow && <div className="mb-1 text-xs font-medium uppercase tracking-wider text-subtle">{eyebrow}</div>}
        <h1 className="font-display text-[28px] leading-tight">{title}</h1>
        {description && <p className="mt-1.5 max-w-3xl text-sm text-muted">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export function Stat({ label, value, sub, info, tone }: { label: ReactNode; value: ReactNode; sub?: ReactNode; info?: string; tone?: Tone }) {
  return (
    <div className="card px-4 py-3">
      <div className="flex items-center gap-1 text-xs text-muted">
        {label}
        {info && <InfoTip term={info} />}
      </div>
      <div className={cx("mt-1 text-2xl font-semibold tabular", tone === "bad" && "text-bad", tone === "warn" && "text-warn", tone === "ok" && "text-ok")}>{value}</div>
      {sub && <div className="mt-0.5 text-xs text-subtle">{sub}</div>}
    </div>
  );
}

export function KV({ items }: { items: [ReactNode, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1.5 text-sm">
      {items.map(([k, v], i) => (
        <div key={i} className="contents">
          <dt className="text-muted">{k}</dt>
          <dd className="min-w-0 break-words">{v}</dd>
        </div>
      ))}
    </dl>
  );
}

/* ---------------------------------------------------------------- states */

export function Skeleton({ className }: { className?: string }) {
  return <div className={cx("skeleton", className)} aria-hidden />;
}

export function Loading({ label = "Loading…", rows = 3 }: { label?: string; rows?: number }) {
  return (
    <div role="status" aria-live="polite" className="space-y-2">
      <span className="sr-only">{label}</span>
      {Array.from({ length: rows }).map((_, i) => (
        <Skeleton key={i} className="h-16 w-full" />
      ))}
    </div>
  );
}

export function EmptyState({ title, children, action, icon }: { title: string; children?: ReactNode; action?: ReactNode; icon?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-border-strong px-6 py-12 text-center">
      <div className="mb-3 text-subtle">{icon ?? <Info className="size-6" aria-hidden />}</div>
      <h3 className="font-medium">{title}</h3>
      {children && <p className="mt-1 max-w-md text-sm text-muted">{children}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function ErrorState({ error, retry }: { error: { message?: string; status?: number } | undefined; retry?: () => void }) {
  if (!error) return null;
  const blocked = error.status === 409;
  return (
    <div role="alert" className={cx("rounded-xl border px-4 py-3 text-sm", blocked ? "border-warn/30 bg-warn-soft text-warn" : "border-bad/30 bg-bad-soft text-bad")}>
      <div className="flex items-center justify-between gap-3">
        <span className="flex items-center gap-2">
          <AlertTriangle className="size-4" aria-hidden />
          {error.message || "Something went wrong."}
        </span>
        {retry && (
          <Button size="sm" onClick={retry}>
            Retry
          </Button>
        )}
      </div>
    </div>
  );
}

export function Notice({ tone = "info", children, title }: { tone?: Tone; children: ReactNode; title?: ReactNode }) {
  return (
    <div className={cx("rounded-lg border px-3.5 py-2.5 text-[13px] leading-relaxed", toneCls[tone])}>
      {title && <div className="mb-0.5 font-semibold">{title}</div>}
      <div className="opacity-90">{children}</div>
    </div>
  );
}

export function Progress({ value, label }: { value: number; label?: string }) {
  const v = Math.max(0, Math.min(1, value || 0));
  return (
    <div role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(v * 100)} aria-label={label ?? "Progress"} className="h-1.5 w-full overflow-hidden rounded-full bg-surface-3">
      <div className="h-full rounded-full bg-accent transition-[width] duration-500" style={{ width: `${v * 100}%` }} />
    </div>
  );
}

/* ---------------------------------------------------------------- info tip */

export function InfoTip({ term }: { term: string }) {
  const t = GLOSSARY[term];
  const [open, setOpen] = useState(false);
  const id = useId();
  const ref = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent | KeyboardEvent) => {
      if (e instanceof KeyboardEvent ? e.key === "Escape" : !ref.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", close);
    };
  }, [open]);
  if (!t) return null;
  return (
    <span ref={ref} className="relative inline-flex">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={id}
        aria-label={`Explain: ${t.title}`}
        onClick={() => setOpen((o) => !o)}
        className="text-subtle hover:text-ink"
      >
        <CircleHelp className="size-3.5" aria-hidden />
      </button>
      {open && (
        <span id={id} role="dialog" aria-label={t.title} className="card absolute left-0 top-6 z-50 w-80 p-3.5 text-left text-xs font-normal leading-relaxed text-ink normal-case tracking-normal">
          <span className="mb-2 block text-[13px] font-semibold">{t.title}</span>
          {(
            [
              ["What is this?", t.what],
              ["How is it calculated?", t.how],
              ["Why does it matter?", t.why],
              ["What should I do?", t.action],
            ] as const
          ).map(([q, a]) => (
            <span key={q} className="mb-2 block last:mb-0">
              <span className="block font-medium text-muted">{q}</span>
              <span className="block">{a}</span>
            </span>
          ))}
        </span>
      )}
    </span>
  );
}

/* ---------------------------------------------------------------- forms */

export function Field({ label, children, hint, error }: { label: string; children: ReactNode; hint?: ReactNode; error?: string | null }) {
  return (
    <label className="block space-y-1">
      <span className="text-[13px] font-medium">{label}</span>
      {children}
      {hint && !error && <span className="block text-xs text-subtle">{hint}</span>}
      {error && <span className="block text-xs text-bad">{error}</span>}
    </label>
  );
}

export const inputCls =
  "w-full rounded-lg border border-border bg-surface px-3 py-2 text-sm placeholder:text-subtle focus:border-accent focus:outline-none";

export function Select({ value, onChange, options, label, className }: { value: string; onChange: (v: string) => void; options: { value: string; label: string }[]; label: string; className?: string }) {
  return (
    <select aria-label={label} value={value} onChange={(e) => onChange(e.target.value)} className={cx("h-8 rounded-lg border border-border bg-surface px-2 text-[13px]", className)}>
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
}

export function Pager({ offset, limit, total, onChange }: { offset: number; limit: number; total: number; onChange: (o: number) => void }) {
  if (total <= limit) return null;
  return (
    <div className="flex items-center justify-between pt-3 text-xs text-muted">
      <span className="tabular">
        {offset + 1}–{Math.min(total, offset + limit)} of {total.toLocaleString()}
      </span>
      <div className="flex gap-1">
        <Button size="sm" variant="ghost" disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - limit))} aria-label="Previous page">
          <ChevronLeft className="size-4" aria-hidden />
        </Button>
        <Button size="sm" variant="ghost" disabled={offset + limit >= total} onClick={() => onChange(offset + limit)} aria-label="Next page">
          <ChevronRight className="size-4" aria-hidden />
        </Button>
      </div>
    </div>
  );
}

export function Tabs<T extends string>({ tabs, value, onChange }: { tabs: { value: T; label: ReactNode; count?: number }[]; value: T; onChange: (v: T) => void }) {
  return (
    <div role="tablist" className="flex flex-wrap gap-1 border-b hairline">
      {tabs.map((t) => (
        <button
          key={t.value}
          role="tab"
          aria-selected={value === t.value}
          onClick={() => onChange(t.value)}
          className={cx("-mb-px border-b-2 px-3 py-2 text-[13px] transition-colors", value === t.value ? "border-accent font-medium text-ink" : "border-transparent text-muted hover:text-ink")}
        >
          {t.label}
          {t.count !== undefined && <span className="ml-1.5 rounded bg-surface-2 px-1.5 text-[11px] tabular text-muted">{t.count}</span>}
        </button>
      ))}
    </div>
  );
}

export function Modal({ open, onClose, title, children, wide }: { open: boolean; onClose: () => void; title: string; children: ReactNode; wide?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);
  return (
    <dialog
      ref={ref}
      onClose={onClose}
      className={cx("card m-auto w-[calc(100%-2rem)] p-0 text-ink backdrop:bg-black/40", wide ? "max-w-3xl" : "max-w-lg")}
      aria-label={title}
    >
      <div className="flex items-center justify-between border-b hairline px-5 py-3">
        <h2 className="font-semibold">{title}</h2>
        <button onClick={onClose} className="text-muted hover:text-ink" aria-label="Close dialog">
          ✕
        </button>
      </div>
      <div className="max-h-[75vh] overflow-y-auto p-5">{children}</div>
    </dialog>
  );
}

/* ---------------------------------------------------------------- samples */

export type SampleLite = { id: string; name: string; label: string; split: string; thumb_url: string | null; path?: string; width?: number; height?: number };

export function SampleThumb({ s, size = 112, caption = true, href, badge }: { s: SampleLite; size?: number; caption?: boolean; href?: string; badge?: ReactNode }) {
  const img = (
    <div className="thumb-checker relative overflow-hidden rounded-lg border hairline" style={{ width: size, height: size }}>
      {s.thumb_url ? (
        <img src={s.thumb_url} alt={`${s.label} sample ${s.name}`} loading="lazy" className="size-full object-cover" />
      ) : (
        <div className="flex size-full items-center justify-center text-xs text-subtle">no preview</div>
      )}
      {badge && <div className="absolute left-1 top-1">{badge}</div>}
    </div>
  );
  return (
    <figure className="min-w-0" style={{ width: size }}>
      {href ? (
        <Link href={href} className="block focus-visible:outline-2">
          {img}
        </Link>
      ) : (
        img
      )}
      {caption && (
        <figcaption className="mt-1 space-y-0.5">
          <div className="truncate text-xs font-medium" title={s.label}>
            {s.label}
          </div>
          <div className="flex items-center gap-1 text-[11px] text-subtle">
            <SplitTag split={s.split} />
            <span className="truncate" title={s.path ?? s.name}>
              {s.name}
            </span>
          </div>
        </figcaption>
      )}
    </figure>
  );
}
