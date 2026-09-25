"use client";

import Link from "next/link";
import { FolderKanban, Plus } from "lucide-react";
import { type FormEvent, useState } from "react";
import { api, useApi } from "@/lib/api";
import { ago } from "@/lib/format";
import { useSession } from "@/lib/session";
import { Button, EmptyState, ErrorState, Field, inputCls, Loading, Modal, PageHeader } from "@/components/ui";

type Project = { id: string; name: string; description: string; datasets: number; created_at: string };

export default function WorkspaceHome() {
  const { org, isAdmin, me } = useSession();
  const { data, error, mutate, isLoading } = useApi<Project[]>(org ? `/orgs/${org.id}/projects` : null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const create = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    setBusy(true);
    setFormError(null);
    try {
      await api(`/orgs/${org!.id}/projects`, { json: { name: f.get("name"), description: f.get("description") } });
      setOpen(false);
      mutate();
    } catch (err: any) {
      setFormError(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mx-auto max-w-6xl px-4 py-8">
      <PageHeader
        eyebrow={org?.name}
        title={`Good to see you${me?.user.name && !me.user.is_demo ? `, ${me.user.name.split(" ")[0]}` : ""}.`}
        description="Projects group datasets that train the same model. Each dataset keeps immutable versions, audits and review history."
        actions={
          isAdmin && (
            <Button variant="primary" onClick={() => setOpen(true)}>
              <Plus className="size-4" aria-hidden /> New project
            </Button>
          )
        }
      />
      <ErrorState error={error} retry={() => mutate()} />
      {isLoading && <Loading rows={2} />}
      {data && data.length === 0 && (
        <EmptyState
          title="No projects yet"
          icon={<FolderKanban className="size-6" aria-hidden />}
          action={
            isAdmin ? (
              <Button variant="primary" onClick={() => setOpen(true)}>
                Create your first project
              </Button>
            ) : undefined
          }
        >
          Create a project, upload an image-classification dataset as a ZIP, and DataCourt will put every sample on trial.
        </EmptyState>
      )}
      <ul className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {(data ?? []).map((p) => (
          <li key={p.id}>
            <Link href={`/app/projects/${p.id}`} className="card block h-full p-5 transition-shadow hover:shadow-md">
              <div className="flex items-center gap-2 text-subtle">
                <FolderKanban className="size-4" aria-hidden />
                <span className="text-xs">{p.datasets} dataset{p.datasets === 1 ? "" : "s"}</span>
              </div>
              <h2 className="mt-2 font-display text-xl">{p.name}</h2>
              {p.description && <p className="mt-1 line-clamp-2 text-sm text-muted">{p.description}</p>}
              <p className="mt-4 text-xs text-subtle">Created {ago(p.created_at)}</p>
            </Link>
          </li>
        ))}
      </ul>
      <Modal open={open} onClose={() => setOpen(false)} title="New project">
        <form onSubmit={create} className="space-y-4">
          <Field label="Name">
            <input name="name" required maxLength={120} className={inputCls} placeholder="e.g. Tomato disease classifier" />
          </Field>
          <Field label="Description (optional)">
            <textarea name="description" rows={3} className={inputCls} />
          </Field>
          {formError && <p className="text-sm text-bad">{formError}</p>}
          <div className="flex justify-end gap-2">
            <Button type="button" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" loading={busy}>
              Create project
            </Button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
