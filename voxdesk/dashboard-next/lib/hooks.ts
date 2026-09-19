"use client";

import { useEffect, useState } from "react";
import { api } from "./api";
import { getToken } from "./auth";

// Resolves the signed-in user's tenant id from /auth/me. It is never derived
// from user input: the tenant binding always comes from the authenticated
// principal, mirroring the server's own rule.
export function useTenantId(): string | null {
  const [tenantId, setTenantId] = useState<string | null>(null);

  useEffect(() => {
    if (!getToken()) return;
    let cancelled = false;
    api
      .me()
      .then((me) => {
        if (!cancelled) setTenantId(me.user.tenant_id);
      })
      .catch(() => {
        // The dashboard shell's guard handles the redirect; a failed /me here
        // simply leaves the value null until the token is restored.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return tenantId;
}
