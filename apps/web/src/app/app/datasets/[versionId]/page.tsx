"use client";

import { useParams, useRouter } from "next/navigation";
import { useEffect } from "react";

export default function VersionIndex() {
  const { versionId } = useParams<{ versionId: string }>();
  const router = useRouter();
  useEffect(() => router.replace(`/app/datasets/${versionId}/overview`), [versionId, router]);
  return null;
}
