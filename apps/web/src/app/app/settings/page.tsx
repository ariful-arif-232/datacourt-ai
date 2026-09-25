"use client";

import { useRouter } from "next/navigation";
import { type FormEvent, useState } from "react";
import { Copy, KeyRound, Trash2, UserPlus } from "lucide-react";
import { Badge, Button, Card, EmptyState, Field, inputCls, KV, Loading, Notice, PageHeader, Select, Tabs } from "@/components/ui";
import { api, clearApiCache, useApi } from "@/lib/api";
import { ago, duration, humanize, num } from "@/lib/format";
import { useSession } from "@/lib/session";

type Tab = "workspace" | "members" | "tokens" | "security" | "danger";
type Msg = { tone: "ok" | "bad"; text: string } | null;

function WorkspaceTab({ org, refresh }: { org: any; refresh: () => void }) {
  const { isAdmin } = useSession();
  const [msg, setMsg] = useState<Msg>(null);
  const submit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    try {
      await api(`/orgs/${org.id}`, {
        method: "PATCH",
        json: { name: String(f.get("name")), privacy_scan: f.get("privacy_scan") === "on", retention_days: Number(f.get("retention_days") || 0) },
      });
      setMsg({ tone: "ok", text: "Saved." });
      refresh();
    } catch (err: any) {
      setMsg({ tone: "bad", text: err.message });
    }
  };
  return (
    <Card title="Workspace settings">
      <form onSubmit={submit} className="max-w-lg space-y-4">
        <Field label="Workspace name">
          <input name="name" defaultValue={org.name} maxLength={120} disabled={!isAdmin} className={inputCls} />
        </Field>
        <label className="flex items-start gap-2 text-sm">
          <input type="checkbox" name="privacy_scan" defaultChecked={!!org.settings?.privacy_scan} disabled={!isAdmin} className="mt-1" />
          <span>
            Run the privacy scan (potential faces and readable text) on new audits.
            <span className="block text-xs text-muted">Detector-based only; no identity recognition. Disable for datasets that legitimately contain people.</span>
          </span>
        </label>
        <Field
          label="Archive retention (days)"
          hint="Leave empty to keep everything. When set, original upload ZIPs (after ingestion), export ZIPs and report files older than this are purged hourly. Dataset versions, findings, decisions and the ledger are never deleted by retention."
        >
          <input name="retention_days" type="number" min={1} max={3650} placeholder="Keep indefinitely" defaultValue={org.retention_days ?? ""} disabled={!isAdmin} className={inputCls} />
        </Field>
        {isAdmin ? <Button type="submit" variant="primary">Save</Button> : <p className="text-xs text-muted">Only owners and admins can change workspace settings.</p>}
        {msg && <Notice tone={msg.tone}>{msg.text}</Notice>}
      </form>
    </Card>
  );
}

function MembersTab({ org, refresh }: { org: any; refresh: () => void }) {
  const { isAdmin, me } = useSession();
  const [role, setRole] = useState("reviewer");
  const [msg, setMsg] = useState<Msg>(null);
  const add = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const form = e.currentTarget;
    const email = String(new FormData(form).get("email"));
    try {
      await api(`/orgs/${org.id}/members`, { json: { email, role } });
      setMsg({ tone: "ok", text: `${email} is now a ${role}.` });
      form.reset();
      refresh();
    } catch (err: any) {
      setMsg({ tone: "bad", text: err.message });
    }
  };
  const remove = async (userId: string, name: string) => {
    if (!window.confirm(`Remove ${name} from ${org.name}?`)) return;
    try {
      await api(`/orgs/${org.id}/members/${userId}`, { method: "DELETE" });
      refresh();
    } catch (err: any) {
      setMsg({ tone: "bad", text: err.message });
    }
  };
  return (
    <div className="grid gap-5 lg:grid-cols-[1fr_360px]">
      <Card title={`Members (${org.members.length})`}>
        <table className="w-full text-sm">
          <thead className="text-left text-xs text-subtle">
            <tr>
              <th className="py-1 font-medium">Name</th>
              <th className="py-1 font-medium">Email</th>
              <th className="py-1 font-medium">Role</th>
              <th className="py-1" />
            </tr>
          </thead>
          <tbody>
            {org.members.map((u: any) => (
              <tr key={u.id} className="border-t hairline">
                <td className="py-2">{u.name}</td>
                <td className="py-2 text-muted">{u.email ?? "hidden"}</td>
                <td className="py-2">
                  <Badge tone={u.role === "owner" ? "accent" : "neutral"}>{u.role}</Badge>
                </td>
                <td className="py-2 text-right">
                  {isAdmin && u.id !== me?.user.id && (
                    <Button size="sm" variant="ghost" onClick={() => remove(u.id, u.name)} aria-label={`Remove ${u.name}`}>
                      <Trash2 className="size-4" aria-hidden />
                    </Button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="mt-4 text-xs text-muted">
          <b className="text-ink">Roles.</b> Owner: everything, including deleting the workspace. Admin: settings, members, tokens, exports, overrides and adjudication. Reviewer: run audits and record review
          decisions. Viewer: read-only.
        </div>
      </Card>
      {isAdmin && (
        <Card title="Add a member" subtitle="The person must already have a DataCourt account.">
          <form onSubmit={add} className="space-y-3">
            <Field label="Email">
              <input name="email" type="email" required className={inputCls} />
            </Field>
            <Select
              label="Role"
              value={role}
              onChange={setRole}
              options={(org.role === "owner" ? ["owner", "admin", "reviewer", "viewer"] : ["admin", "reviewer", "viewer"]).map((r) => ({ value: r, label: humanize(r) }))}
            />
            <Button type="submit" variant="primary" className="w-full">
              <UserPlus className="size-4" aria-hidden /> Add or update member
            </Button>
          </form>
          {msg && <div className="mt-3"><Notice tone={msg.tone}>{msg.text}</Notice></div>}
        </Card>
      )}
    </div>
  );
}

const SCOPES = [
  { id: "contracts:read", label: "contracts:read — query the CI data-contract gate" },
  { id: "audits:read", label: "audits:read — read audit results" },
  { id: "audits:write", label: "audits:write — start audits" },
];

function TokensTab({ org }: { org: any }) {
  const { data: tokens, mutate } = useApi<any[]>(`/orgs/${org.id}/tokens`);
  const [scopes, setScopes] = useState<string[]>(["contracts:read"]);
  const [created, setCreated] = useState<string | null>(null);
  const [msg, setMsg] = useState<Msg>(null);
  const create = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const form = e.currentTarget;
    try {
      const r = await api(`/orgs/${org.id}/tokens`, { json: { name: String(new FormData(form).get("name")), scopes } });
      setCreated(r.token);
      form.reset();
      mutate();
    } catch (err: any) {
      setMsg({ tone: "bad", text: err.message });
    }
  };
  const revoke = async (id: string) => {
    if (!window.confirm("Revoke this token? CI jobs using it will stop working.")) return;
    await api(`/orgs/${org.id}/tokens/${id}`, { method: "DELETE" });
    mutate();
  };
  return (
    <div className="grid gap-5 lg:grid-cols-[1fr_360px]">
      <Card title="API tokens" subtitle="Tokens are hashed at rest; only the prefix is shown after creation.">
        {!tokens ? (
          <Loading rows={2} />
        ) : tokens.length === 0 ? (
          <EmptyState title="No tokens" icon={<KeyRound className="size-5" />} />
        ) : (
          <ul className="divide-y divide-border text-sm">
            {tokens.map((t) => (
              <li key={t.id} className="flex flex-wrap items-center gap-2 py-2.5">
                <span className="font-medium">{t.name}</span>
                <span className="font-mono text-xs text-muted">{t.prefix}…</span>
                {t.scopes.map((s: string) => (
                  <Badge key={s}>{s}</Badge>
                ))}
                {t.revoked && <Badge tone="bad">revoked</Badge>}
                <span className="text-xs text-subtle">created {ago(t.created_at)} · last used {t.last_used_at ? ago(t.last_used_at) : "never"}</span>
                <div className="flex-1" />
                {!t.revoked && (
                  <Button size="sm" variant="ghost" onClick={() => revoke(t.id)}>
                    Revoke
                  </Button>
                )}
              </li>
            ))}
          </ul>
        )}
      </Card>
      <Card title="Create token">
        <form onSubmit={create} className="space-y-3">
          <Field label="Name">
            <input name="name" required maxLength={80} placeholder="e.g. GitHub Actions" className={inputCls} />
          </Field>
          <fieldset className="space-y-1.5 text-sm">
            <legend className="mb-1 text-xs font-medium text-muted">Scopes</legend>
            {SCOPES.map((s) => (
              <label key={s.id} className="flex items-start gap-2">
                <input
                  type="checkbox"
                  className="mt-1"
                  checked={scopes.includes(s.id)}
                  onChange={(e) => setScopes((cur) => (e.target.checked ? [...cur, s.id] : cur.filter((x) => x !== s.id)))}
                />
                <span className="text-xs">{s.label}</span>
              </label>
            ))}
          </fieldset>
          <Button type="submit" variant="primary" className="w-full" disabled={scopes.length === 0}>
            Create token
          </Button>
        </form>
        {created && (
          <div className="mt-3 space-y-2">
            <Notice tone="warn" title="Copy this token now — it will not be shown again.">
              <code className="block break-all font-mono text-xs">{created}</code>
            </Notice>
            <Button size="sm" onClick={() => navigator.clipboard?.writeText(created)}>
              <Copy className="size-4" aria-hidden /> Copy
            </Button>
          </div>
        )}
        {msg && <div className="mt-3"><Notice tone={msg.tone}>{msg.text}</Notice></div>}
      </Card>
    </div>
  );
}

function SecurityTab({ org }: { org: any }) {
  const { data: log } = useApi<any[]>(`/orgs/${org.id}/security-log`);
  const { data: metrics } = useApi<any>(`/orgs/${org.id}/metrics`);
  return (
    <div className="grid gap-5 lg:grid-cols-[1fr_320px]">
      <Card title="Security audit log" subtitle="Authentication, membership, token, setting and deletion events for this workspace.">
        {!log ? (
          <Loading rows={3} />
        ) : log.length === 0 ? (
          <EmptyState title="No security events yet" />
        ) : (
          <ul className="max-h-[480px] divide-y divide-border overflow-y-auto text-sm">
            {log.map((e, i) => (
              <li key={i} className="flex flex-wrap items-center gap-2 py-2">
                <span className="font-mono text-xs">{e.action}</span>
                {e.target_type && <span className="text-xs text-muted">{e.target_type}</span>}
                <div className="flex-1" />
                <span className="text-xs text-subtle">{ago(e.created_at)}</span>
              </li>
            ))}
          </ul>
        )}
      </Card>
      <Card title="Operations">
        {!metrics ? (
          <Loading rows={2} />
        ) : (
          <KV
            items={[
              ["Audits run", num(metrics.audits_run)],
              ["Median audit time", duration(metrics.median_audit_seconds)],
              ["Jobs queued", num(metrics.queue_length)],
              ["Jobs by status", Object.entries(metrics.jobs_by_status).map(([k, v]) => `${k} ${v}`).join(" · ") || "—"],
              ["Failures by class", Object.entries(metrics.failures_by_class).map(([k, v]) => `${k} ${v}`).join(" · ") || "none"],
            ]}
          />
        )}
      </Card>
    </div>
  );
}

function DangerTab({ org }: { org: any }) {
  const { me } = useSession();
  const router = useRouter();
  const [msg, setMsg] = useState<Msg>(null);
  const deleteOrg = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const confirm_name = String(new FormData(e.currentTarget).get("confirm_name"));
    try {
      const r = await api(`/orgs/${org.id}`, { method: "DELETE", json: { confirm_name } });
      setMsg({ tone: "ok", text: `Workspace deleted (${r.deleted_objects} stored objects removed).` });
      await clearApiCache();
      router.push("/app");
    } catch (err: any) {
      setMsg({ tone: "bad", text: err.message });
    }
  };
  const deleteAccount = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const confirm_email = String(new FormData(e.currentTarget).get("confirm_email"));
    try {
      await api("/auth/account", { method: "DELETE", json: { confirm_email } });
      await clearApiCache();
      router.replace("/");
    } catch (err: any) {
      setMsg({ tone: "bad", text: err.message });
    }
  };
  return (
    <div className="grid gap-5 lg:grid-cols-2">
      {org.role === "owner" && (
        <Card title="Delete workspace" subtitle="Permanently deletes every project, dataset version, uploaded file, derived artifact, review decision and export in this workspace.">
          <form onSubmit={deleteOrg} className="space-y-3">
            <Field label={`Type the workspace name (${org.name}) to confirm`}>
              <input name="confirm_name" required className={inputCls} autoComplete="off" />
            </Field>
            <Button type="submit" variant="danger">
              Delete workspace permanently
            </Button>
          </form>
        </Card>
      )}
      <Card
        title="Delete my account"
        subtitle="Deletes every workspace where you are the only owner, with all of its data. If you reviewed cases in other workspaces, your account is pseudonymised instead: those decisions stay, attributed to “Deleted user”."
      >
        <form onSubmit={deleteAccount} className="space-y-3">
          <Field label={`Type your email (${me?.user.email}) to confirm`}>
            <input name="confirm_email" type="email" required className={inputCls} autoComplete="off" />
          </Field>
          <Button type="submit" variant="danger">
            Delete my account
          </Button>
        </form>
      </Card>
      {msg && <Notice tone={msg.tone}>{msg.text}</Notice>}
    </div>
  );
}

export default function SettingsPage() {
  const { org: current, me, refresh } = useSession();
  const { data: org, mutate, error } = useApi<any>(current ? `/orgs/${current.id}` : null);
  const [tab, setTab] = useState<Tab>("workspace");
  if (me?.user.is_demo)
    return (
      <>
        <PageHeader title="Settings" />
        <Notice tone="info">You are exploring the read-only demo workspace. Create a free account to manage your own workspace, members and API tokens.</Notice>
      </>
    );
  const reload = () => {
    mutate();
    refresh();
  };
  const isAdmin = org && (org.role === "owner" || org.role === "admin");
  return (
    <>
      <PageHeader title="Settings" description={org ? `${org.name} · your role: ${org.role}` : undefined} />
      {error ? (
        <Notice tone="bad">{error.message}</Notice>
      ) : !org ? (
        <Loading />
      ) : (
        <div className="space-y-5">
          <Tabs<Tab>
            value={tab}
            onChange={setTab}
            tabs={[
              { value: "workspace", label: "Workspace" },
              { value: "members", label: "Members", count: org.members.length },
              ...(isAdmin ? [{ value: "tokens" as Tab, label: "API tokens" }, { value: "security" as Tab, label: "Security & operations" }] : []),
              { value: "danger", label: "Data deletion" },
            ]}
          />
          {tab === "workspace" && <WorkspaceTab org={org} refresh={reload} />}
          {tab === "members" && <MembersTab org={org} refresh={reload} />}
          {tab === "tokens" && isAdmin && <TokensTab org={org} />}
          {tab === "security" && isAdmin && <SecurityTab org={org} />}
          {tab === "danger" && <DangerTab org={org} />}
        </div>
      )}
    </>
  );
}
