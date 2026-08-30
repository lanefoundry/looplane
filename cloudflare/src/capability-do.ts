import { DurableObject } from "cloudflare:workers";

export interface CapabilityIdentity {
  modelProfile: string;
  provider: string;
  model: string;
  profileFingerprint: string;
}

interface CapabilityRecord extends CapabilityIdentity {
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

const IDENTITY_KEYS = ["modelProfile", "provider", "model", "profileFingerprint"] as const;

function isBoundedString(value: unknown, maxLength: number): value is string {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= maxLength &&
    value.trim() === value &&
    !/[\u0000-\u001f\u007f]/u.test(value)
  );
}

function isCapabilityIdentity(
  value: Record<string, unknown>,
): value is CapabilityIdentity & Record<string, unknown> {
  return (
    isBoundedString(value.modelProfile, 128) &&
    isBoundedString(value.provider, 64) &&
    isBoundedString(value.model, 256) &&
    isBoundedString(value.profileFingerprint, 128)
  );
}

function hasExactKeys(value: Record<string, unknown>, expected: readonly string[]): boolean {
  const keys = Object.keys(value);
  return keys.length === expected.length && keys.every((key) => expected.includes(key));
}

function identityMatches(record: CapabilityRecord, identity: CapabilityIdentity): boolean {
  return (
    record.modelProfile === identity.modelProfile &&
    record.provider === identity.provider &&
    record.model === identity.model &&
    record.profileFingerprint === identity.profileFingerprint
  );
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
        !isCapabilityIdentity(body) ||
        typeof body.expiresAt !== "number" ||
        !Number.isInteger(body.expiresAt) ||
        body.expiresAt <= now ||
        body.expiresAt > now + 305_000 ||
        typeof body.maxRequests !== "number" ||
        !Number.isInteger(body.maxRequests) ||
        body.maxRequests < 1 ||
        body.maxRequests > 64 ||
        !hasExactKeys(body, [...IDENTITY_KEYS, "expiresAt", "maxRequests"])
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
          modelProfile: body.modelProfile,
          provider: body.provider,
          model: body.model,
          profileFingerprint: body.profileFingerprint,
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
        !isCapabilityIdentity(body) ||
        !hasExactKeys(body, IDENTITY_KEYS)
      ) {
        return json({ error: "invalid_consume" }, 400);
      }
      const now = Date.now();
      let outcome: CapabilityConsumeResult = "inactive";
      let remaining = 0;
      await this.ctx.storage.transaction(async (transaction) => {
        const record = await transaction.get<CapabilityRecord>("capability");
        if (record === undefined || !identityMatches(record, body)) return;
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
        !isCapabilityIdentity(body) ||
        !hasExactKeys(body, IDENTITY_KEYS)
      ) {
        return json({ error: "invalid_check" }, 400);
      }
      const now = Date.now();
      const record = await this.ctx.storage.get<CapabilityRecord>("capability");
      if (record === undefined || !identityMatches(record, body)) {
        return json({ error: "inactive" }, 401);
      }
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
  identity: CapabilityIdentity,
  expiresAt: number,
  maxRequests: number,
): Promise<void> {
  const response = await stub(namespace, runId).fetch("https://capability.internal/activate", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ ...identity, expiresAt, maxRequests }),
  });
  if (response.status !== 201) throw new Error("run capability activation failed");
}

export async function consumeCapability(
  namespace: DurableObjectNamespace<RunCapability>,
  runId: string,
  identity: CapabilityIdentity,
): Promise<CapabilityConsumeResult> {
  const response = await stub(namespace, runId).fetch("https://capability.internal/consume", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(identity),
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
  identity: CapabilityIdentity,
): Promise<CapabilityConsumeResult> {
  const response = await stub(namespace, runId).fetch("https://capability.internal/check", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(identity),
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
