"use client";

import { useEffect, useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { getToken } from "@/lib/auth";
import ShellNav from "./shell-nav";

export default function DashboardShell({ children }: { children: ReactNode }) {
  const router = useRouter();
  const [ready, setReady] = useState(false);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    setReady(true);
  }, [router]);

  if (!ready) {
    return <div className="loading">Loading…</div>;
  }

  return (
    <div className="shell">
      <ShellNav />
      <main className="content">{children}</main>
    </div>
  );
}
