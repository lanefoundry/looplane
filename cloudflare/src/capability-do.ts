import { DurableObject } from "cloudflare:workers";

interface CapabilityRecord {
  model: string;
  expiresAt: number;
  maxRequests: number;
  usedRequests: number;
}

export type CapabilityConsumeResult = "ok" | "inactive" | "expired" | "exhausted";

function json(value: unknown, status = 200): Response {
  return Response.json(value, {
    status,
    headers: { "cache-control": "no-store", "x-content-type-options": "nosniff" },
  });
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function parseBody(request: Request): Promise<Record<string, unknown> | null> {
  try {
    const value: unknown = await request.json();
    return isObject(value) ? value : null;
  } catch {
    return null;
  }
}

/** Strongly consistent, per-run capability budget. Only reachable through its DO binding. */
export class RunCapability extends DurableObject<unknown> {
  constructor(ctx: DurableObjectState, env: unknown) {
    super(ctx, env);
  }

  async fetch(request: Request): Promise<Response> {
    const path = new URL(request.url).pathname;
    if (request.method !== "POST") return json({ error: "method_not_allowed" }, 405);

    if (path === "/activate") {
      const body = await parseBody(request);
      const now = Date.now();
      if (
        body === null ||
        typeof body.model !== "string" ||
        body.model.length < 1 ||
        body.model.length > 256 ||
        typeof body.expiresAt !== "number" ||
        !Number.isInteger(body.expiresAt) ||
        body.expiresAt <= now ||
        body.expiresAt > now + 305_000 ||
        typeof body.maxRequests !== "number" ||
        !Number.isInteger(body.maxRequests) ||
        body.maxRequests < 1 ||
        body.maxRequests > 64 ||
        Object.keys(body).some((key) => !["model", "expiresAt", "maxRequests"].includes(key))
      ) {
        return json({ error: "invalid_activation" }, 400);
      }
      let conflict = false;
      await this.ctx.storage.transaction(async (transaction) => {
        const existing = await transaction.get<CapabilityRecord>("capability");
        if (existing !== undefined && existing.expiresAt > now) {
          conflict = true;
          return;
        }
        await transaction.put<CapabilityRecord>("capability", {
          model: body.model as string,
          expiresAt: body.expiresAt as number,
          maxRequests: body.maxRequests as number,
          usedRequests: 0,
        });
      });
      if (conflict) return json({ error: "already_active" }, 409);
      return json({ ok: true }, 201);
    }

    if (path === "/consume") {
      const body = await parseBody(request);
      if (
        body === null ||
        typeof body.model !== "string" ||
        Object.keys(body).some((key) => key !== "model")
      ) {
        return json({ error: "invalid_consume" }, 400);
      }
      const now = Date.now();
      let outcome: CapabilityConsumeResult = "inactive";
      let remaining = 0;
      await this.ctx.storage.transaction(async (transaction) => {
        const record = await transaction.get<CapabilityRecord>("capability");
        if (record === undefined || record.model !== body.model) return;
        if (record.expiresAt <= now) {
          outcome = "expired";
          await transaction.delete("capability");
          return;
        }
        if (record.usedRequests >= record.maxRequests) {
          outcome = "exhausted";
          return;
        }
        record.usedRequests += 1;
        remaining = record.maxRequests - record.usedRequests;
        outcome = "ok";
        await transaction.put("capability", record);
      });
      if (outcome === "inactive") return json({ error: outcome }, 401);
      if (outcome === "expired") return json({ error: outcome }, 401);
      if (outcome === "exhausted") return json({ error: outcome }, 429);
      return json({ ok: true, remaining });
    }

    if (path === "/check") {
      const body = await parseBody(request);
      if (
        body === null ||
        typeof body.model !== "string" ||
        Object.keys(body).some((key) => key !== "model")
      ) {
        return json({ error: "invalid_check" }, 400);
      }
      const now = Date.now();
      const record = await this.ctx.storage.get<CapabilityRecord>("capability");
      if (record === undefined || record.model !== body.model) return json({ error: "inactive" }, 401);
      if (record.expiresAt <= now) {
        await this.ctx.storage.delete("capability");
        return json({ error: "expired" }, 401);
      }
      if (record.usedRequests >= record.maxRequests) return json({ error: "exhausted" }, 429);
      return json({ ok: true, remaining: record.maxRequests - record.usedRequests });
    }

    if (path === "/revoke") {
      await this.ctx.storage.delete("capability");
      return new Response(null, { status: 204 });
    }

    return json({ error: "not_found" }, 404);
  }
}

function stub(namespace: DurableObjectNamespace<RunCapability>, runId: string): DurableObjectStub {
  return namespace.get(namespace.idFromName(runId));
}

export async function activateCapability(
  namespace: DurableObjectNamespace<RunCapability>,
  runId: string,
  model: string,
  expiresAt: number,
  maxRequests: number,
): Promise<void> {
  const response = await stub(namespace, runId).fetch("https://capability.internal/activate", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ model, expiresAt, maxRequests }),
  });
  if (response.status !== 201) throw new Error("run capability activation failed");
}

export async function consumeCapability(
  namespace: DurableObjectNamespace<RunCapability>,
  runId: string,
  model: string,
): Promise<CapabilityConsumeResult> {
  const response = await stub(namespace, runId).fetch("https://capability.internal/consume", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ model }),
  });
  if (response.status === 200) return "ok";
  if (response.status === 429) return "exhausted";
  if (response.status === 401) {
    try {
      const body: unknown = await response.json();
      if (isObject(body) && body.error === "expired") return "expired";
    } catch {
      // Fail closed as inactive below.
    }
    return "inactive";
  }
  throw new Error("run capability consumption failed");
}

export async function checkCapability(
  namespace: DurableObjectNamespace<RunCapability>,
  runId: string,
  model: string,
): Promise<CapabilityConsumeResult> {
  const response = await stub(namespace, runId).fetch("https://capability.internal/check", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ model }),
  });
  if (response.status === 200) return "ok";
  if (response.status === 429) return "exhausted";
  if (response.status === 401) {
    try {
      const body: unknown = await response.json();
      if (isObject(body) && body.error === "expired") return "expired";
    } catch {
      // Fail closed as inactive below.
    }
    return "inactive";
  }
  throw new Error("run capability check failed");
}

export async function revokeCapability(
  namespace: DurableObjectNamespace<RunCapability>,
  runId: string,
): Promise<void> {
  const response = await stub(namespace, runId).fetch("https://capability.internal/revoke", {
    method: "POST",
  });
  if (response.status !== 204) throw new Error("run capability revocation failed");
}
