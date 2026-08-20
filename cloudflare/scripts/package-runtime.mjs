import { mkdir, rm } from "node:fs/promises";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const cloudflareDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repositoryDir = path.resolve(cloudflareDir, "..");
const outputDir = path.join(cloudflareDir, ".artifacts");

await rm(outputDir, { recursive: true, force: true });
await mkdir(outputDir, { recursive: true });

async function runUv(args) {
  await new Promise((resolve, reject) => {
    const child = spawn("uv", args, {
      cwd: repositoryDir,
      stdio: "inherit",
    });
    child.once("error", reject);
    child.once("exit", (code) => {
      if (code === 0) resolve();
      else reject(new Error(`uv ${args[0]} exited with status ${code ?? "unknown"}`));
    });
  });
}

await runUv(["build", "--wheel", "--out-dir", outputDir]);
await runUv([
  "export",
  "--frozen",
  "--no-dev",
  "--extra",
  "sandbox",
  "--no-emit-project",
  "--format",
  "requirements.txt",
  "--no-header",
  "--no-annotate",
  "--quiet",
  "--output-file",
  path.join(outputDir, "requirements.txt"),
]);
await runUv([
  "export",
  "--frozen",
  "--no-dev",
  "--extra",
  "sandbox",
  "--no-emit-project",
  "--format",
  "cyclonedx1.5",
  "--quiet",
  "--output-file",
  path.join(outputDir, "python-dependencies.cdx.json"),
]);
