import { afterEach, describe, expect, it, vi } from "vitest";
import { RunSession } from "../src/run-session-do";

const now = Date.UTC(2026, 7, 21, 0, 0, 0);

class FakeStorage {
  private readonly values = new Map<string, unknown>();

  async get<T>(key: string): Promise<T | undefined> {
    return this.values.get(key) as T | undefined;
  }

  async put<T>(key: string, value: T): Promise<void> {
    this.values.set(key, value);
  }

  async transaction<T>(callback: (transaction: FakeStorage) => Promise<T>): Promise<T> {
    return callback(this);
  }
}

function durableObject(): RunSession {
  return new RunSession(
    { storage: new FakeStorage() } as unknown as DurableObjectState,
    undefined,
  );
}

function post(path: string, body?: unknown): Request {
  return new Request(`https://run-session.internal${path}`, {
    method: "POST",
    headers: body === undefined ? undefined : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

function get(path: string, init?: RequestInit): Request {
  return new Request(`https://run-session.internal${path}`, init);
}

function createBody(): Record<string, unknown> {
  return {
    runId: "run-1",
    model: "gpt-5-mini",
    createdAt: now,
    request: {
      instruction: "Fix hello.py",
      model: "gpt-5-mini",
      allowedPaths: ["hello.py"],
      checks: [["git", "diff", "--check"]],
      fileCount: 1,
    },
  };
}

function terminalBody(): Record<string, unknown> {
  return {
    execution: { success: true, exitCode: 0 },
    output: {
      ok: true,
      result: {
        status: "completed",
        summary: "fixed",
        terminal_reason: "verified",
      },
      artifacts: {
        events: "{\"event_type\":\"run.completed\"}\n",
        patch: "diff --git a/hello.py b/hello.py\n",
      },
    },
    artifacts: {
      events: "{\"event_type\":\"run.completed\"}\n",
      patch: "diff --git a/hello.py b/hello.py\n",
    },
  };
}

function eventLine(sequence: number): string {
  return JSON.stringify({
    event_type: sequence === 0 ? "run.created" : "run.completed",
    run_id: "agent-run-id",
    task_id: "run-1",
    sequence,
    data: {},
  }) + "\n";
}

function approvalLine(sequence: number, type = "approval.requested"): string {
  return JSON.stringify({
    event_type: type,
    run_id: "agent-run-id",
    task_id: "run-1",
    sequence,
    data: {
      request_id: "approval-1",
      action_id: "action-1",
      effect: "execute",
      reason: "model_tool",
      policy_reason: "suspicious command",
      preview: "Run pytest",
    },
  }) + "\n";
}

async function readChunk(
  reader: ReadableStreamDefaultReader<Uint8Array>,
): Promise<ReadableStreamReadResult<Uint8Array>> {
  return await Promise.race([
    reader.read(),
    new Promise<ReadableStreamReadResult<Uint8Array>>((_, reject) => {
      setTimeout(() => reject(new Error("timed out waiting for stream chunk")), 1_000);
    }),
  ]);
}

function decode(chunk: Uint8Array | undefined): string {
  return chunk === undefined ? "" : new TextDecoder().decode(chunk);
}

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("RunSession Durable Object", () => {
  it("persists lifecycle metadata without exposing artifact bodies in status", async () => {
    vi.spyOn(Date, "now").mockReturnValue(now + 1);
    const session = durableObject();

    expect((await session.fetch(post("/create", createBody()))).status).toBe(201);
    expect((await session.fetch(post("/running"))).status).toBe(200);
    expect((await session.fetch(post("/complete", terminalBody()))).status).toBe(200);

    const status = (await (await session.fetch(get("/status"))).json()) as Record<string, unknown>;
    expect(status).toMatchObject({
      runId: "run-1",
      status: "completed",
      model: "gpt-5-mini",
      summary: "fixed",
      terminalReason: "verified",
      artifactKeys: ["events", "patch"],
    });
    expect(JSON.stringify(status)).not.toContain("diff --git");
    expect(JSON.stringify(status)).not.toContain("run.completed");
    expect(await (await session.fetch(get("/events"))).text()).toBe(
      "{\"event_type\":\"run.completed\"}\n",
    );
    expect(await (await session.fetch(get("/artifacts/patch"))).text()).toBe(
      "diff --git a/hello.py b/hello.py\n",
    );
  });

  it("marks cancel requested and keeps terminal cancellation idempotent", async () => {
    vi.spyOn(Date, "now").mockReturnValue(now + 1);
    const session = durableObject();

    expect((await session.fetch(post("/create", createBody()))).status).toBe(201);
    expect((await session.fetch(post("/running"))).status).toBe(200);
    const first = await session.fetch(post("/cancel"));
    const second = await session.fetch(post("/cancel"));

    expect(first.status).toBe(202);
    expect(await first.json()).toEqual({ ok: true, status: "cancelled" });
    expect(second.status).toBe(200);
    expect(await second.json()).toEqual({ ok: true, status: "cancelled" });
    const status = (await (await session.fetch(get("/status"))).json()) as Record<string, unknown>;
    expect(status).toMatchObject({ status: "cancelled", cancelRequested: true });
  });

  it("appends live event lines before terminal completion", async () => {
    vi.spyOn(Date, "now").mockReturnValue(now + 1);
    const session = durableObject();

    expect((await session.fetch(post("/create", createBody()))).status).toBe(201);
    expect((await session.fetch(post("/running"))).status).toBe(200);
    const append = await session.fetch(
      post("/append-events", { lines: [eventLine(0), eventLine(1)] }),
    );

    expect(append.status).toBe(200);
    expect(await append.json()).toEqual({ ok: true, lines: 2 });
    expect(await (await session.fetch(get("/events"))).text()).toBe(eventLine(0) + eventLine(1));
    const stream = await session.fetch(get("/events?stream=1"));
    const reader = stream.body!.getReader();
    const first = await readChunk(reader);
    const streamText = decode(first.value);
    expect(stream.headers.get("content-type")).toContain("text/event-stream");
    expect(streamText).toContain("id: 0\nevent: run.created\ndata: ");
    expect(streamText).toContain("id: 1\nevent: run.completed\ndata: ");
    expect(streamText).toContain(eventLine(0).trim());
    await reader.cancel();
  });

  it("stores pending approvals from events and records submitted decisions", async () => {
    vi.spyOn(Date, "now").mockReturnValue(now + 1);
    const session = durableObject();

    expect((await session.fetch(post("/create", createBody()))).status).toBe(201);
    expect((await session.fetch(post("/running"))).status).toBe(200);
    expect((await session.fetch(post("/append-events", { lines: [approvalLine(0)] }))).status).toBe(
      200,
    );

    const listed = await session.fetch(get("/approvals"));
    expect(await listed.json()).toEqual({
      pending: [
        {
          requestId: "approval-1",
          actionId: "action-1",
          effect: "execute",
          reason: "model_tool",
          policyReason: "suspicious command",
          preview: "Run pytest",
          requestedAt: now + 1,
        },
      ],
      decisions: [],
    });

    const submitted = await session.fetch(
      post("/approvals/approval-1", { decision: "allow_once" }),
    );
    expect(submitted.status).toBe(200);
    expect(await submitted.json()).toEqual({
      ok: true,
      requestId: "approval-1",
      decision: "allow_once",
    });
    expect(await (await session.fetch(get("/approvals"))).json()).toEqual({
      pending: [],
      decisions: [
        {
          requestId: "approval-1",
          decision: "allow_once",
          decidedAt: now + 1,
        },
      ],
    });
  });

  it("streams appended events to active subscribers and closes on terminal completion", async () => {
    const session = durableObject();

    expect((await session.fetch(post("/create", createBody()))).status).toBe(201);
    expect((await session.fetch(post("/running"))).status).toBe(200);
    expect((await session.fetch(post("/append-events", { lines: [eventLine(0)] }))).status).toBe(200);
    const stream = await session.fetch(get("/events?stream=1"));
    const reader = stream.body!.getReader();
    const replay = await readChunk(reader);
    expect(replay.done).toBe(false);
    expect(decode(replay.value)).toContain("id: 0\nevent: run.created\ndata: ");
    const pendingRead = readChunk(reader);

    expect((await session.fetch(post("/append-events", { lines: [eventLine(1)] }))).status).toBe(200);
    const pushed = await pendingRead;
    expect(pushed.done).toBe(false);
    expect(decode(pushed.value)).toContain("id: 1\nevent: run.completed\ndata: ");

    expect((await session.fetch(post("/complete", terminalBody()))).status).toBe(200);
    const closed = await readChunk(reader);
    expect(closed.done).toBe(true);
  });

  it("resumes SSE replay after Last-Event-ID", async () => {
    const session = durableObject();

    expect((await session.fetch(post("/create", createBody()))).status).toBe(201);
    expect((await session.fetch(post("/running"))).status).toBe(200);
    expect(
      (await session.fetch(post("/append-events", { lines: [eventLine(0), eventLine(1)] }))).status,
    ).toBe(200);
    const stream = await session.fetch(
      get("/events?stream=1", { headers: { "last-event-id": "0" } }),
    );
    const reader = stream.body!.getReader();
    const replay = await readChunk(reader);
    const text = decode(replay.value);

    expect(replay.done).toBe(false);
    expect(text).not.toContain("id: 0\nevent: run.created");
    expect(text).toContain("id: 1\nevent: run.completed");
    await reader.cancel();
  });

  it("keeps idle SSE subscribers alive with heartbeat comments and clears them on cancel", async () => {
    vi.useFakeTimers();
    const session = durableObject();

    expect((await session.fetch(post("/create", createBody()))).status).toBe(201);
    expect((await session.fetch(post("/running"))).status).toBe(200);
    const stream = await session.fetch(get("/events?stream=1"));
    const reader = stream.body!.getReader();
    const pendingRead = reader.read();

    await vi.advanceTimersByTimeAsync(15_000);
    const heartbeat = await pendingRead;
    expect(heartbeat.done).toBe(false);
    expect(decode(heartbeat.value)).toBe(": heartbeat\n\n");

    await reader.cancel();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("closes active subscribers on failure and cancellation", async () => {
    for (const terminalPath of ["/fail", "/cancel"]) {
      const session = durableObject();
      expect((await session.fetch(post("/create", createBody()))).status).toBe(201);
      expect((await session.fetch(post("/running"))).status).toBe(200);
      const stream = await session.fetch(get("/events?stream=1"));
      const reader = stream.body!.getReader();
      const pendingRead = readChunk(reader);

      if (terminalPath === "/fail") {
        expect((await session.fetch(post("/fail", { error: "boom" }))).status).toBe(200);
      } else {
        expect((await session.fetch(post("/cancel"))).status).toBe(202);
      }

      const closed = await pendingRead;
      expect(closed.done).toBe(true);
    }
  });

  it("rejects malformed live event lines, appends after terminal status, and late completion", async () => {
    const session = durableObject();

    expect((await session.fetch(post("/create", createBody()))).status).toBe(201);
    for (const line of [
      "\n",
      "[]\n",
      "null\n",
      "{\"event_type\":\"x\"}\n",
      eventLine(0).replace(/\n$/u, ""),
      eventLine(0).replace(/\n$/u, "\n\n"),
      eventLine(0).replace("\"task_id\":\"run-1\"", "\"task_id\":\"other\""),
    ]) {
      expect((await session.fetch(post("/append-events", { lines: [line] }))).status).toBe(400);
    }
    expect((await session.fetch(post("/append-events", { lines: [eventLine(0)] }))).status).toBe(200);
    expect((await session.fetch(post("/complete", terminalBody()))).status).toBe(200);
    expect((await session.fetch(post("/append-events", { lines: [eventLine(1)] }))).status).toBe(409);
    expect((await session.fetch(post("/complete", terminalBody()))).status).toBe(409);
  });

  it("keeps cancelled sessions from being overwritten by late completion", async () => {
    const session = durableObject();

    expect((await session.fetch(post("/create", createBody()))).status).toBe(201);
    expect((await session.fetch(post("/running"))).status).toBe(200);
    expect((await session.fetch(post("/cancel"))).status).toBe(202);
    expect((await session.fetch(post("/complete", terminalBody()))).status).toBe(409);
    const status = (await (await session.fetch(get("/status"))).json()) as Record<string, unknown>;
    expect(status.status).toBe("cancelled");
  });

  it("rejects illegal transitions and unknown artifacts", async () => {
    const session = durableObject();

    expect((await session.fetch(post("/running"))).status).toBe(404);
    expect((await session.fetch(post("/create", createBody()))).status).toBe(201);
    expect((await session.fetch(post("/complete", terminalBody()))).status).toBe(200);
    expect((await session.fetch(post("/running"))).status).toBe(409);
    expect((await session.fetch(get("/artifacts/result"))).status).toBe(404);
  });
});
