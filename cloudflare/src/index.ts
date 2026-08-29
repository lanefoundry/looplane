import { getSandbox, streamFile } from "@cloudflare/sandbox";
import {
  activateCapability,
  checkCapability,
  consumeCapability,
  revokeCapability,
} from "./capability-do";
import type { Env, WorkerDependencies } from "./control-plane";
import { handleRequest } from "./control-plane";
import {
  appendRunSessionEvents,
  cancelRunSession,
  completeRunSession,
  createRunSession,
  failRunSession,
  getRunSession,
  getRunSessionArtifact,
  getRunSessionEvents,
  markRunSessionRunning,
} from "./run-session-do";

export { Sandbox } from "@cloudflare/sandbox";
export { RunCapability } from "./capability-do";
export { RunSession } from "./run-session-do";

const dependencies: WorkerDependencies = {
  getSandbox: (env, id) => getSandbox(env.Sandbox, id),
  fetch: (input, init) => fetch(input, init),
  now: () => Date.now(),
  randomUUID: () => crypto.randomUUID(),
  activateCapability: (env, runId, model, expiresAt, maxRequests) =>
    activateCapability(env.RUN_CAPABILITIES, runId, model, expiresAt, maxRequests),
  checkCapability: (env, runId, model) => checkCapability(env.RUN_CAPABILITIES, runId, model),
  consumeCapability: (env, runId, model) =>
    consumeCapability(env.RUN_CAPABILITIES, runId, model),
  revokeCapability: (env, runId) => revokeCapability(env.RUN_CAPABILITIES, runId),
  createRunSession: (env, runId, request, createdAt) =>
    createRunSession(env.RUN_SESSIONS, runId, request, createdAt),
  markRunSessionRunning: (env, runId) => markRunSessionRunning(env.RUN_SESSIONS, runId),
  completeRunSession: (env, runId, execution, output) =>
    completeRunSession(env.RUN_SESSIONS, runId, execution, output),
  appendRunSessionEvents: (env, runId, lines) =>
    appendRunSessionEvents(env.RUN_SESSIONS, runId, lines),
  failRunSession: (env, runId, error) => failRunSession(env.RUN_SESSIONS, runId, error),
  cancelRunSession: (env, runId) => cancelRunSession(env.RUN_SESSIONS, runId),
  getRunSession: (env, runId) => getRunSession(env.RUN_SESSIONS, runId),
  getRunSessionEvents: (env, runId, stream, lastEventId) =>
    getRunSessionEvents(env.RUN_SESSIONS, runId, stream, lastEventId),
  getRunSessionArtifact: (env, runId, name) =>
    getRunSessionArtifact(env.RUN_SESSIONS, runId, name),
  decodeFileStream: (stream) => streamFile(stream),
};

export default {
  fetch(request: Request, env: Env): Promise<Response> {
    return handleRequest(request, env, dependencies);
  },
} satisfies ExportedHandler<Env>;
