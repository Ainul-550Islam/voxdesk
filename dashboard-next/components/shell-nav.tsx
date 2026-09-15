"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { clearToken } from "@/lib/auth";

// Every section whose page has landed in this dashboard. Order matches the
// Vite dashboard's navigation so the two apps stay comparable during the
// strangler migration.
const LINKS = [
  { href: "/dashboard", label: "Overview" },
  { href: "/dashboard/calls", label: "Calls" },
  { href: "/dashboard/analytics", label: "Analytics" },
  { href: "/dashboard/appointments", label: "Appointments" },
  { href: "/dashboard/campaigns", label: "Campaigns" },
  { href: "/dashboard/leads", label: "Leads" },
  { href: "/dashboard/knowledge", label: "Knowledge" },
  { href: "/dashboard/integrations", label: "Integrations" },
  { href: "/dashboard/agent", label: "Agent" },
  { href: "/dashboard/team", label: "Team" },
  { href: "/dashboard/billing", label: "Billing" },
  { href: "/dashboard/audit", label: "Audit" },
];

export default function ShellNav() {
  const pathname = usePathname();
  const router = useRouter();

  function signOut() {
    clearToken();
    router.replace("/login");
  }

  function isActive(href: string): boolean {
    if (href === "/dashboard") return pathname === "/dashboard";
    return pathname === href || pathname.startsWith(`${href}/`);
  }

  return (
    <nav className="sidebar">
      <div className="brand">VoxDesk</div>
      <ul>
        {LINKS.map((link) => (
          <li key={link.href}>
            <Link href={link.href} className={isActive(link.href) ? "active" : undefined}>
              {link.label}
            </Link>
          </li>
        ))}
      </ul>
      <button className="sign-out" type="button" onClick={signOut}>
        Sign out
      </button>
    </nav>
  );
}
