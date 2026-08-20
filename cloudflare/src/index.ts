import { getSandbox } from "@cloudflare/sandbox";
import {
  activateCapability,
  consumeCapability,
  revokeCapability,
} from "./capability-do";
import type { Env, WorkerDependencies } from "./control-plane";
import { handleRequest } from "./control-plane";

export { Sandbox } from "@cloudflare/sandbox";
export { RunCapability } from "./capability-do";

const dependencies: WorkerDependencies = {
  getSandbox: (env, id) => getSandbox(env.Sandbox, id),
  fetch: (input, init) => fetch(input, init),
  now: () => Date.now(),
  randomUUID: () => crypto.randomUUID(),
  activateCapability: (env, runId, model, expiresAt, maxRequests) =>
    activateCapability(env.RUN_CAPABILITIES, runId, model, expiresAt, maxRequests),
  consumeCapability: (env, runId, model) =>
    consumeCapability(env.RUN_CAPABILITIES, runId, model),
  revokeCapability: (env, runId) => revokeCapability(env.RUN_CAPABILITIES, runId),
};

export default {
  fetch(request: Request, env: Env): Promise<Response> {
    return handleRequest(request, env, dependencies);
  },
} satisfies ExportedHandler<Env>;
