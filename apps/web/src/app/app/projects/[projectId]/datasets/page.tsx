"use client";

import { useParams, useRouter } from "next/navigation";
import { useEffect } from "react";

export default function DatasetsIndex() {
  const { projectId } = useParams<{ projectId: string }>();
  const router = useRouter();
  useEffect(() => router.replace(`/app/projects/${projectId}`), [projectId, router]);
  return null;
}
