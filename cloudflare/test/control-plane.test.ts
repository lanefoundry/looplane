import { describe, expect, it, vi } from "vitest";
import {
  createRunToken,
  destroySandboxBounded,
  handleRequest,
  LIMITS,
  revokeCapabilityBounded,
  validatedModelApiUrl,
  type Env,
  type SandboxHandle,
  type WorkerDependencies,
} from "../src/control-plane";

const now = Date.UTC(2026, 7, 21, 0, 0, 0);
const controlToken = "control-plane-token-with-enough-bytes";
const runSecret = "run-token-secret-with-at-least-thirty-two-bytes";
const providerSecret = "provider-secret-must-stay-in-worker";
const model = "gpt-5-mini";

function env(): Env {
  return {
    Sandbox: {} as Env["Sandbox"],
    RUN_CAPABILITIES: {} as Env["RUN_CAPABILITIES"],
    CONTROL_PLANE_TOKEN: controlToken,
    RUN_TOKEN_SECRET: runSecret,
    OPENAI_API_KEY: providerSecret,
    OPENAI_MODEL: model,
    MODEL_API_URL: "https://api.openai.com/v1/chat/completions",
  };
}

const runId = "11111111-1111-4111-8111-111111111111";

function validSandboxResponse(status: "completed" | "failed" | "cancelled" = "completed"): Record<string, unknown> {
  const artifactPaths = {
    request: "/workspace/runs/run/request.json",
    events: "/workspace/runs/run/events.jsonl",
    checkpoint: "/workspace/runs/run/checkpoint.json",
    patch: "/workspace/runs/run/changes.patch",
    test_log: "/workspace/runs/run/test.log",
    result: "/workspace/runs/run/result.json",
  };
  return {
    ok: status === "completed",
    result: {
      run_id: "agent-run-id",
      task_id: runId,
      status,
      summary: "fixed",
      changed_files: ["hello.py"],
      verification: [
        {
          name: "check-1",
          argv: ["git", "diff", "--check"],
          ok: true,
          exit_code: 0,
          duration_seconds: 0.1,
          output: "",
        },
      ],
      usage: {
        input_tokens: 10,
        output_tokens: 5,
        cached_input_tokens: 0,
        reasoning_tokens: 0,
        provider_total_tokens: 15,
        total_tokens: 15,
      },
      terminal_reason: "verified",
      artifacts: artifactPaths,
    },
    artifacts: {
      request: "{}",
      events: "{}\n",
      checkpoint: "{}",
      patch: "diff --git a/hello.py b/hello.py\n",
      test_log: "ok",
      result: "{}",
    },
  };
}

function requestBody(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    instruction: "Change hello.py without touching other files.",
    model,
    files: [{ path: "hello.py", content: "print('hello')\n" }],
    allowedPaths: ["hello.py"],
    checks: [["git", "diff", "--check"]],
    ...overrides,
  };
}

function request(path: string, body: unknown, token = controlToken): Request {
  return new Request(`https://control.example${path}`, {
    method: "POST",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

function sandbox(response: unknown = validSandboxResponse()): SandboxHandle & {
  mkdir: ReturnType<typeof vi.fn>;
  writeFile: ReturnType<typeof vi.fn>;
  exec: ReturnType<typeof vi.fn>;
  readFileStream: ReturnType<typeof vi.fn>;
  destroy: ReturnType<typeof vi.fn>;
} {
  return {
    mkdir: vi.fn().mockResolvedValue({ success: true }),
    writeFile: vi.fn().mockResolvedValue({ success: true }),
    exec: vi.fn().mockResolvedValue({ success: true, exitCode: 0, stdout: "", stderr: "" }),
    readFileStream: vi.fn().mockResolvedValue(
      new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(new TextEncoder().encode(JSON.stringify(response)));
          controller.close();
        },
      }),
    ),
    destroy: vi.fn().mockResolvedValue(undefined),
  };
}

function dependencies(handle: SandboxHandle): WorkerDependencies {
  return {
    getSandbox: vi.fn().mockReturnValue(handle),
    fetch: vi.fn(),
    now: () => now,
    randomUUID: () => runId,
    activateCapability: vi.fn().mockResolvedValue(undefined),
    consumeCapability: vi.fn().mockResolvedValue("ok"),
    revokeCapability: vi.fn().mockResolvedValue(undefined),
    decodeFileStream: async function* (stream) {
      const reader = stream.getReader();
      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) return;
          yield value;
        }
      } finally {
        reader.releaseLock();
      }
    },
  };
}

describe("POST /v1/runs", () => {
  it("exposes a minimal unauthenticated health route", async () => {
    const response = await handleRequest(
      new Request("https://control.example/healthz"),
      env(),
      dependencies(sandbox()),
    );
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ ok: true, service: "rivumi-control-plane" });
  });

  it("requires control-plane authentication before allocating a sandbox", async () => {
    const handle = sandbox();
    const deps = dependencies(handle);
    const response = await handleRequest(request("/v1/runs", requestBody(), "wrong"), env(), deps);

    expect(response.status).toBe(401);
    expect(deps.getSandbox).not.toHaveBeenCalled();
  });

  it("stages bounded text, runs only the fixed entrypoint, reads the result, and destroys", async () => {
    const handle = sandbox();
    const deps = dependencies(handle);
    const response = await handleRequest(request("/v1/runs", requestBody()), env(), deps);

    expect(response.status).toBe(201);
    expect(handle.writeFile).toHaveBeenCalledWith(
      "/workspace/source/hello.py",
      "print('hello')\n",
    );
    expect(handle.exec).toHaveBeenCalledTimes(1);
    const [command, options] = handle.exec.mock.calls[0]!;
    expect(command).toBe("/usr/local/bin/rivumi-sandbox-run");
    expect(options.env).toMatchObject({
      RIVUMI_MODEL_ID: model,
      RIVUMI_MODEL_GATEWAY_URL: "https://control.example/internal/v1",
    });
    expect(JSON.stringify(options.env)).not.toContain(providerSecret);
    expect(options.env).not.toHaveProperty("RIVUMI_RUN_TOKEN");
    const tokenWrite = handle.writeFile.mock.calls.find(
      ([path]) => path === "/workspace/.rivumi-run-token",
    );
    expect(tokenWrite?.[1]).toEqual(expect.any(String));
    expect(handle.readFileStream).toHaveBeenCalledWith("/workspace/response.json");
    expect(deps.activateCapability).toHaveBeenCalledWith(
      expect.anything(),
      runId,
      model,
      now + LIMITS.runTokenSeconds * 1_000,
      14,
    );
    expect(deps.revokeCapability).toHaveBeenCalledWith(expect.anything(), runId);
    expect(handle.destroy).toHaveBeenCalledTimes(1);
  });

  it.each([
    { files: [{ path: "../escape.py", content: "x = 1" }] },
    { allowedPaths: ["missing.py"] },
    { checks: [["sh", "-c", "pytest"]] },
    { model: "another-model" },
  ])("rejects an unsafe contract before sandbox allocation: %j", async (override) => {
    const handle = sandbox();
    const deps = dependencies(handle);
    const response = await handleRequest(request("/v1/runs", requestBody(override)), env(), deps);

    expect(response.status).toBe(400);
    expect(deps.getSandbox).not.toHaveBeenCalled();
  });

  it("enforces per-file UTF-8 bounds", async () => {
    const handle = sandbox();
    const deps = dependencies(handle);
    const response = await handleRequest(
      request("/v1/runs", requestBody({ files: [{ path: "big.txt", content: "界".repeat(LIMITS.fileBytes) }] })),
      env(),
      deps,
    );

    expect(response.status).toBe(413);
    expect(deps.getSandbox).not.toHaveBeenCalled();
  });

  it("cancels an oversized streamed ingress body without relying on Content-Length", async () => {
    const handle = sandbox();
    const deps = dependencies(handle);
    const cancel = vi.fn();
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new Uint8Array(500_000));
        controller.enqueue(new Uint8Array(300_001));
      },
      cancel,
    });
    const streamedRequest = new Request("https://control.example/v1/runs", {
      method: "POST",
      headers: { authorization: `Bearer ${controlToken}`, "content-type": "application/json" },
      body,
      duplex: "half",
    } as RequestInit & { duplex: "half" });

    const response = await handleRequest(streamedRequest, env(), deps);

    expect(response.status).toBe(413);
    expect(cancel).toHaveBeenCalled();
    expect(deps.getSandbox).not.toHaveBeenCalled();
  });

  it("destroys the sandbox when execution fails", async () => {
    const handle = sandbox();
    handle.exec.mockRejectedValue(new Error("container unavailable"));
    const deps = dependencies(handle);
    const response = await handleRequest(request("/v1/runs", requestBody()), env(), deps);

    expect(response.status).toBe(500);
    expect(deps.revokeCapability).toHaveBeenCalledWith(expect.anything(), runId);
    expect(handle.destroy).toHaveBeenCalledTimes(1);
  });

  it.each(["mkdir", "writeFile", "exec"] as const)(
    "fails closed when Sandbox %s returns success false",
    async (operation) => {
      const handle = sandbox();
      handle[operation].mockResolvedValue({
        success: false,
        exitCode: 1,
        stdout: "",
        stderr: "failed",
      });
      const deps = dependencies(handle);

      const response = await handleRequest(request("/v1/runs", requestBody()), env(), deps);

      expect(response.status).toBe(502);
      expect(deps.revokeCapability).toHaveBeenCalledWith(expect.anything(), runId);
      expect(handle.destroy).toHaveBeenCalledTimes(1);
    },
  );

  it("returns a strictly matched terminal agent failure with full artifacts", async () => {
    const handle = sandbox(validSandboxResponse("failed"));
    handle.exec.mockResolvedValue({
      success: false,
      exitCode: 1,
      stdout: "",
      stderr: "",
    });

    const response = await handleRequest(
      request("/v1/runs", requestBody()),
      env(),
      dependencies(handle),
    );
    const body = (await response.json()) as Record<string, any>;

    expect(response.status).toBe(201);
    expect(body.execution).toEqual({ success: false, exitCode: 1 });
    expect(body.output.ok).toBe(false);
    expect(body.output.result.status).toBe("failed");
  });

  it("rejects exit zero paired with a failed terminal response", async () => {
    const handle = sandbox(validSandboxResponse("failed"));
    const response = await handleRequest(
      request("/v1/runs", requestBody()),
      env(),
      dependencies(handle),
    );

    expect(response.status).toBe(502);
  });

  it("fails closed when the bounded result stream cannot be opened", async () => {
    const handle = sandbox();
    handle.readFileStream.mockRejectedValue(new Error("missing response"));

    const response = await handleRequest(
      request("/v1/runs", requestBody()),
      env(),
      dependencies(handle),
    );

    expect(response.status).toBe(502);
    expect(handle.destroy).toHaveBeenCalledTimes(1);
  });

  it("decodes the Sandbox SDK SSE stream before parsing the response JSON", async () => {
    const handle = sandbox();
    handle.readFileStream.mockResolvedValue(
      new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(new TextEncoder().encode("event: file_chunk\ndata: encoded\n\n"));
          controller.close();
        },
      }),
    );
    const deps = dependencies(handle);
    deps.decodeFileStream = vi.fn().mockImplementation(async function* () {
      yield new TextEncoder().encode(JSON.stringify(validSandboxResponse()));
    });

    const response = await handleRequest(request("/v1/runs", requestBody()), env(), deps);

    expect(response.status).toBe(201);
    expect(deps.decodeFileStream).toHaveBeenCalledTimes(1);
  });

  it("bounds the decoded Sandbox file rather than its transport framing", async () => {
    const handle = sandbox();
    const deps = dependencies(handle);
    deps.decodeFileStream = vi.fn().mockImplementation(async function* () {
      yield new Uint8Array(LIMITS.runResponseBytes);
      yield new Uint8Array(1);
    });

    const response = await handleRequest(request("/v1/runs", requestBody()), env(), deps);

    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({ error: "sandbox_response_too_large" });
  });

  it.each(["sandbox_entrypoint_failed", "sandbox_agent_failed"])(
    "preserves the bounded Sandbox failure class: %s",
    async (error) => {
      const handle = sandbox({ ok: false, error });
      handle.exec.mockResolvedValue({ success: false, exitCode: 1, stdout: "", stderr: "" });

      const response = await handleRequest(
        request("/v1/runs", requestBody()),
        env(),
        dependencies(handle),
      );

      expect(response.status).toBe(502);
      expect(await response.json()).toEqual({ error });
    },
  );

  it.each([
    { ...validSandboxResponse(), injected: "value" },
    { ok: true, artifacts: validSandboxResponse().artifacts },
    { ...validSandboxResponse(), ok: false },
    {
      ...validSandboxResponse(),
      result: { ...(validSandboxResponse().result as Record<string, unknown>), task_id: "other" },
    },
  ])("rejects a forged or malformed Sandbox response: %j", async (forged) => {
    const handle = sandbox(forged);
    const response = await handleRequest(
      request("/v1/runs", requestBody()),
      env(),
      dependencies(handle),
    );

    expect(response.status).toBe(502);
    expect(handle.destroy).toHaveBeenCalledTimes(1);
  });

  it.each([
    {
      result: {
        ...(validSandboxResponse().result as Record<string, unknown>),
        verification: [],
      },
    },
    {
      result: {
        ...(validSandboxResponse().result as Record<string, unknown>),
        verification: [
          {
            name: "check-1",
            argv: ["python3", "-m", "pytest", "-q"],
            ok: true,
            exit_code: 0,
            duration_seconds: 0.1,
            output: "",
          },
        ],
      },
    },
    {
      result: {
        ...(validSandboxResponse().result as Record<string, unknown>),
        verification: [
          {
            name: "check-1",
            argv: ["git", "diff", "--check"],
            ok: false,
            exit_code: 1,
            duration_seconds: 0.1,
            output: "failed",
          },
        ],
      },
    },
    {
      result: {
        ...(validSandboxResponse().result as Record<string, unknown>),
        changed_files: ["other.py"],
      },
    },
  ])("rejects a completed result that does not match its request contract: %j", async (resultOverride) => {
    const forged = { ...validSandboxResponse(), ...resultOverride };
    const handle = sandbox(forged);

    const response = await handleRequest(
      request("/v1/runs", requestBody()),
      env(),
      dependencies(handle),
    );

    expect(response.status).toBe(502);
  });

  it("does not claim completion when destroy rejects", async () => {
    const handle = sandbox();
    handle.destroy.mockRejectedValue(new Error("destroy failed"));
    const deps = dependencies(handle);

    const response = await handleRequest(request("/v1/runs", requestBody()), env(), deps);

    expect(response.status).toBe(500);
    expect(await response.json()).toEqual({ error: "sandbox_cleanup_failed" });
    expect(deps.revokeCapability).toHaveBeenCalledBefore(handle.destroy);
  });

  it("bounds a hanging destroy call", async () => {
    const handle = sandbox();
    handle.destroy.mockReturnValue(new Promise<void>(() => undefined));

    await expect(destroySandboxBounded(handle, 10)).rejects.toMatchObject({
      code: "sandbox_cleanup_timeout",
    });
  });

  it("bounds a hanging capability revocation before destroying", async () => {
    const handle = sandbox();
    const deps = dependencies(handle);
    deps.revokeCapability = vi.fn().mockReturnValue(new Promise<void>(() => undefined));

    await expect(revokeCapabilityBounded(deps, env(), runId, 10)).rejects.toMatchObject({
      code: "sandbox_cleanup_timeout",
    });
  });

  it("revokes the capability so a captured token cannot proxy after teardown", async () => {
    const handle = sandbox();
    let active = false;
    const deps = dependencies(handle);
    deps.activateCapability = vi.fn().mockImplementation(async () => {
      active = true;
    });
    deps.revokeCapability = vi.fn().mockImplementation(async () => {
      active = false;
    });
    deps.consumeCapability = vi.fn().mockImplementation(async () =>
      active ? "ok" : "inactive",
    );

    expect((await handleRequest(request("/v1/runs", requestBody()), env(), deps)).status).toBe(201);
    const token = handle.writeFile.mock.calls.find(
      ([path]) => path === "/workspace/.rivumi-run-token",
    )?.[1] as string;
    const replay = await handleRequest(
      request(
        "/internal/v1/chat/completions",
        { model, messages: [{ role: "user", content: "late" }] },
        token,
      ),
      env(),
      deps,
    );

    expect(replay.status).toBe(401);
    expect(deps.fetch).not.toHaveBeenCalled();
  });
});

describe("internal model proxy", () => {
  it("accepts a short-lived capability, pins the model, and keeps the provider key upstream", async () => {
    const handle = sandbox();
    const deps = dependencies(handle);
    const upstream = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ choices: [{ message: { content: "ok" } }] }), {
        headers: { "content-type": "application/json" },
      }),
    );
    deps.fetch = upstream;
    const token = await createRunToken(runSecret, "run-1", model, Math.floor(now / 1000));
    const capability = JSON.parse(
      Buffer.from(token.split(".")[0]!, "base64url").toString("utf8"),
    ) as Record<string, unknown>;
    expect(capability).toMatchObject({
      aud: "/internal/v1/chat/completions",
      runId: "run-1",
      model,
    });
    const response = await handleRequest(
      request(
        "/internal/v1/chat/completions",
        { model, messages: [{ role: "user", content: "hello" }] },
        token,
      ),
      env(),
      deps,
    );

    expect(response.status).toBe(200);
    expect(upstream).toHaveBeenCalledTimes(1);
    const [url, init] = upstream.mock.calls[0]!;
    expect(url).toBe("https://api.openai.com/v1/chat/completions");
    expect(init.headers.authorization).toBe(`Bearer ${providerSecret}`);
    expect(JSON.parse(init.body)).toMatchObject({ model, stream: false, max_tokens: 4096 });
    expect(await response.text()).not.toContain(providerSecret);
  });

  it("rejects an expired run capability before calling the provider", async () => {
    const handle = sandbox();
    const deps = dependencies(handle);
    const token = await createRunToken(
      runSecret,
      "run-1",
      model,
      Math.floor(now / 1000) - LIMITS.runTokenSeconds - 1,
    );
    const response = await handleRequest(
      request(
        "/internal/v1/chat/completions",
        { model, messages: [{ role: "user", content: "hello" }] },
        token,
      ),
      env(),
      deps,
    );

    expect(response.status).toBe(401);
    expect(deps.fetch).not.toHaveBeenCalled();
  });

  it("cancels an oversized streamed provider response without Content-Length", async () => {
    const handle = sandbox();
    const deps = dependencies(handle);
    const cancel = vi.fn();
    deps.fetch = vi.fn().mockResolvedValue(
      new Response(
        new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(new Uint8Array(700_000));
            controller.enqueue(new Uint8Array(400_001));
          },
          cancel,
        }),
        { headers: { "content-type": "application/json" } },
      ),
    );
    const token = await createRunToken(runSecret, "run-1", model, Math.floor(now / 1000));

    const response = await handleRequest(
      request(
        "/internal/v1/chat/completions",
        { model, messages: [{ role: "user", content: "hello" }] },
        token,
      ),
      env(),
      deps,
    );

    expect(response.status).toBe(502);
    expect(cancel).toHaveBeenCalled();
  });

  it("rejects extra OpenAI fields and model substitution", async () => {
    const handle = sandbox();
    const deps = dependencies(handle);
    const token = await createRunToken(runSecret, "run-1", model, Math.floor(now / 1000));
    const response = await handleRequest(
      request(
        "/internal/v1/chat/completions",
        { model: "gpt-4o", messages: [{ role: "user", content: "hello" }], stream: true },
        token,
      ),
      env(),
      deps,
    );

    expect(response.status).toBe(400);
    expect(deps.fetch).not.toHaveBeenCalled();
  });
});

describe("operator model endpoint", () => {
  it("accepts an operator-owned OpenAI-compatible HTTPS endpoint", () => {
    expect(validatedModelApiUrl("https://openrouter.ai/api/v1/chat/completions")).toBe(
      "https://openrouter.ai/api/v1/chat/completions",
    );
  });

  it.each([
    "http://api.groq.com/openai/v1/chat/completions",
    "https://user:password@api.example/v1/chat/completions",
    "https://api.example/v1/chat/completions?key=secret",
    "https://api.example/v1/chat/completions#fragment",
  ])("rejects an unsafe operator endpoint: %s", (url) => {
    expect(() => validatedModelApiUrl(url)).toThrow();
  });
});
