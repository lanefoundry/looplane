import { afterEach, describe, expect, it, vi } from "vitest";
import { RunCapability } from "../src/capability-do";

const now = Date.UTC(2026, 7, 21, 0, 0, 0);

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
            model: "gpt-5-mini",
            expiresAt: now + 300_000,
            maxRequests: 2,
          }),
        )
      ).status,
    ).toBe(201);
    expect((await capability.fetch(post("/consume", { model: "gpt-5-mini" }))).status).toBe(200);
    expect((await capability.fetch(post("/consume", { model: "gpt-5-mini" }))).status).toBe(200);
    expect((await capability.fetch(post("/consume", { model: "gpt-5-mini" }))).status).toBe(429);
    expect((await capability.fetch(post("/revoke"))).status).toBe(204);
    expect((await capability.fetch(post("/consume", { model: "gpt-5-mini" }))).status).toBe(401);
  });

  it("rejects model substitution and an overlapping activation", async () => {
    vi.spyOn(Date, "now").mockReturnValue(now);
    const capability = durableObject();
    const activation = {
      model: "gpt-5-mini",
      expiresAt: now + 300_000,
      maxRequests: 4,
    };

    expect((await capability.fetch(post("/activate", activation))).status).toBe(201);
    expect((await capability.fetch(post("/activate", activation))).status).toBe(409);
    expect((await capability.fetch(post("/consume", { model: "gpt-4o" }))).status).toBe(401);
  });

  it("deletes expired capability state before denying use", async () => {
    const clock = vi.spyOn(Date, "now").mockReturnValue(now);
    const capability = durableObject();
    expect(
      (
        await capability.fetch(
          post("/activate", {
            model: "gpt-5-mini",
            expiresAt: now + 1_000,
            maxRequests: 1,
          }),
        )
      ).status,
    ).toBe(201);
    clock.mockReturnValue(now + 1_001);

    expect((await capability.fetch(post("/consume", { model: "gpt-5-mini" }))).status).toBe(401);
    expect((await capability.fetch(post("/consume", { model: "gpt-5-mini" }))).status).toBe(401);
  });
});
