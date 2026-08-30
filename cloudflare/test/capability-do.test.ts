import { afterEach, describe, expect, it, vi } from "vitest";
import { RunCapability } from "../src/capability-do";

const now = Date.UTC(2026, 7, 21, 0, 0, 0);
const identity = {
  modelProfile: "openrouter-primary",
  provider: "openrouter",
  model: "openai/gpt-5-mini",
  profileFingerprint: "profile-sha256-7d1b",
};

class FakeStorage {
  private readonly values = new Map<string, unknown>();

  async get<T>(key: string): Promise<T | undefined> {
    return this.values.get(key) as T | undefined;
  }

  async put<T>(key: string, value: T): Promise<void> {
    this.values.set(key, value);
  }

  async delete(key: string): Promise<boolean> {
    return this.values.delete(key);
  }

  async transaction<T>(callback: (transaction: FakeStorage) => Promise<T>): Promise<T> {
    return callback(this);
  }
}

function durableObject(): RunCapability {
  return new RunCapability(
    { storage: new FakeStorage() } as unknown as DurableObjectState,
    undefined,
  );
}

function post(path: string, body?: unknown): Request {
  return new Request(`https://capability.internal${path}`, {
    method: "POST",
    headers: body === undefined ? undefined : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

afterEach(() => vi.restoreAllMocks());

describe("RunCapability Durable Object", () => {
  it("atomically bounds model requests and then revokes the run", async () => {
    vi.spyOn(Date, "now").mockReturnValue(now);
    const capability = durableObject();

    expect(
      (
        await capability.fetch(
          post("/activate", {
            ...identity,
            expiresAt: now + 300_000,
            maxRequests: 2,
          }),
        )
      ).status,
    ).toBe(201);
    expect((await capability.fetch(post("/consume", identity))).status).toBe(200);
    expect((await capability.fetch(post("/consume", identity))).status).toBe(200);
    expect((await capability.fetch(post("/consume", identity))).status).toBe(429);
    expect((await capability.fetch(post("/revoke"))).status).toBe(204);
    expect((await capability.fetch(post("/consume", identity))).status).toBe(401);
  });

  it("checks an active capability without consuming request budget", async () => {
    vi.spyOn(Date, "now").mockReturnValue(now);
    const capability = durableObject();
    const activation = {
      ...identity,
      expiresAt: now + 300_000,
      maxRequests: 1,
    };

    expect((await capability.fetch(post("/activate", activation))).status).toBe(201);
    expect((await capability.fetch(post("/check", identity))).status).toBe(200);
    expect((await capability.fetch(post("/check", identity))).status).toBe(200);
    expect((await capability.fetch(post("/consume", identity))).status).toBe(200);
    expect((await capability.fetch(post("/consume", identity))).status).toBe(429);
  });

  it("rejects every profile identity substitution and an overlapping activation", async () => {
    vi.spyOn(Date, "now").mockReturnValue(now);
    const capability = durableObject();
    const activation = {
      ...identity,
      expiresAt: now + 300_000,
      maxRequests: 4,
    };

    expect((await capability.fetch(post("/activate", activation))).status).toBe(201);
    expect((await capability.fetch(post("/activate", activation))).status).toBe(409);
    for (const substitution of [
      { ...identity, modelProfile: "other" },
      { ...identity, provider: "other" },
      { ...identity, model: "openai/gpt-4o" },
      { ...identity, profileFingerprint: "other-fingerprint" },
    ]) {
      expect((await capability.fetch(post("/check", substitution))).status).toBe(401);
      expect((await capability.fetch(post("/consume", substitution))).status).toBe(401);
    }
    expect((await capability.fetch(post("/consume", identity))).status).toBe(200);
  });

  it("strictly validates the complete identity on every operation", async () => {
    vi.spyOn(Date, "now").mockReturnValue(now);
    const capability = durableObject();
    const activation = { ...identity, expiresAt: now + 300_000, maxRequests: 1 };

    for (const key of Object.keys(identity)) {
      const incomplete = { ...activation } as Record<string, unknown>;
      delete incomplete[key];
      expect((await capability.fetch(post("/activate", incomplete))).status).toBe(400);
    }
    expect(
      (await capability.fetch(post("/activate", { ...activation, apiKey: "forbidden" }))).status,
    ).toBe(400);
    expect((await capability.fetch(post("/activate", activation))).status).toBe(201);

    expect((await capability.fetch(post("/check", { ...identity, extra: true }))).status).toBe(400);
    expect((await capability.fetch(post("/consume", { ...identity, provider: "" }))).status).toBe(
      400,
    );
  });

  it("deletes expired capability state before denying use", async () => {
    const clock = vi.spyOn(Date, "now").mockReturnValue(now);
    const capability = durableObject();
    expect(
      (
        await capability.fetch(
          post("/activate", {
            ...identity,
            expiresAt: now + 1_000,
            maxRequests: 1,
          }),
        )
      ).status,
    ).toBe(201);
    clock.mockReturnValue(now + 1_001);

    expect((await capability.fetch(post("/consume", identity))).status).toBe(401);
    expect((await capability.fetch(post("/consume", identity))).status).toBe(401);
  });
});
