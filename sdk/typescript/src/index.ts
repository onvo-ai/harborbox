export type SandboxStatus =
  | "created"
  | "starting"
  | "running"
  | "paused_memory"
  | "paused_cold"
  | "killed"
  | "failed";

type ExecutionStatus =
  | "queued"
  | "admitted"
  | "starting"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled";

interface SandboxPayload {
  id: string;
  status: SandboxStatus;
  memory_mb: number;
  cpu: number;
  idle_timeout_seconds: number;
  metadata: Record<string, string>;
}

interface ExecutionPayload {
  id: string;
  status: ExecutionStatus;
  logs: {
    stdout: string[];
    stderr: string[];
    truncated: boolean;
  } | null;
  error: {
    name: string;
    value: string;
    traceback: string[];
  } | null;
  exit_code: number | null;
}

export interface SandboxCreateOptions {
  timeoutMs?: number;
  memoryMb?: number;
  cpu?: number;
  baseUrl?: string;
  apiKey?: string;
  metadata?: Record<string, string>;
}

export interface CommandOptions {
  timeoutMs?: number;
  envs?: Record<string, string>;
  cwd?: string;
}

export interface CommandResult {
  stdout: string;
  stderr: string;
  exitCode: number;
  error?: {
    name: string;
    value: string;
    traceback: string[];
  };
}

interface ProcessLike {
  env?: Record<string, string | undefined>;
}

function environment(): Record<string, string | undefined> {
  return (
    globalThis as typeof globalThis & { process?: ProcessLike }
  ).process?.env ?? {};
}

function sleep(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

class HarborboxConnection {
  readonly baseUrl: string;
  readonly apiKey: string;

  constructor(options: SandboxCreateOptions) {
    const env = environment();
    this.baseUrl = (
      options.baseUrl ??
      env.HARBORBOX_BASE_URL ??
      "http://127.0.0.1:8000"
    ).replace(/\/$/, "");
    this.apiKey = options.apiKey ?? env.HARBORBOX_API_KEY ?? "";
    if (!this.apiKey) {
      throw new Error("HARBORBOX_API_KEY or apiKey is required");
    }
  }

  async request<T>(
    path: string,
    init: RequestInit = {},
  ): Promise<T> {
    const headers = new Headers(init.headers);
    headers.set("X-API-Key", this.apiKey);
    const response = await fetch(`${this.baseUrl}${path}`, {
      ...init,
      headers,
    });
    if (!response.ok) {
      const detail = await response.text();
      throw new Error(
        `Harborbox ${response.status} ${response.statusText}: ${detail}`,
      );
    }
    if (response.status === 204) {
      return undefined as T;
    }
    return (await response.json()) as T;
  }

  async waitForExecution(
    executionId: string,
    timeoutMs: number,
  ): Promise<ExecutionPayload> {
    const deadline = Date.now() + timeoutMs;
    while (true) {
      const execution = await this.request<ExecutionPayload>(
        `/v1/executions/${executionId}`,
      );
      if (
        execution.status === "succeeded" ||
        execution.status === "failed" ||
        execution.status === "cancelled"
      ) {
        return execution;
      }
      if (Date.now() >= deadline) {
        throw new Error(`execution ${executionId} did not finish in time`);
      }
      await sleep(200);
    }
  }
}

class Commands {
  constructor(private readonly sandbox: Sandbox) {}

  async run(
    command: string,
    options: CommandOptions = {},
  ): Promise<CommandResult> {
    const timeoutMs = options.timeoutMs ?? 30_000;
    const execution = await this.sandbox.connection.request<ExecutionPayload>(
      `/v1/sandboxes/${this.sandbox.sandboxId}/commands`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          command,
          timeout_seconds: Math.max(1, Math.ceil(timeoutMs / 1000)),
          env: options.envs ?? {},
          cwd: options.cwd ?? null,
        }),
      },
    );
    const completed = await this.sandbox.connection.waitForExecution(
      execution.id,
      timeoutMs + 60_000,
    );
    this.sandbox.status = "running";
    const result: CommandResult = {
      stdout: (completed.logs?.stdout ?? []).join(""),
      stderr: (completed.logs?.stderr ?? []).join(""),
      exitCode: completed.exit_code ?? (completed.status === "succeeded" ? 0 : 1),
    };
    if (completed.error) {
      result.error = completed.error;
    }
    return result;
  }
}

class Files {
  constructor(private readonly sandbox: Sandbox) {}

  async write(
    path: string,
    data: string | ArrayBuffer | ArrayBufferView,
  ): Promise<void> {
    await this.sandbox.ensureReady();
    if (typeof data === "string") {
      await this.sandbox.connection.request(
        `/v1/sandboxes/${this.sandbox.sandboxId}/files`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path, content: data, encoding: "utf-8" }),
        },
      );
      return;
    }

    const bytes =
      data instanceof ArrayBuffer
        ? new Uint8Array(data)
        : new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
    await this.sandbox.connection.request(
      `/v1/sandboxes/${this.sandbox.sandboxId}/files/content?path=${encodeURIComponent(path)}`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/octet-stream" },
        body: bytes as unknown as BodyInit,
      },
    );
  }

  async read(path: string): Promise<string> {
    await this.sandbox.ensureReady();
    const payload = await this.sandbox.connection.request<{
      content: string;
      encoding: "utf-8" | "base64";
    }>(
      `/v1/sandboxes/${this.sandbox.sandboxId}/files?path=${encodeURIComponent(path)}`,
    );
    if (payload.encoding === "utf-8") {
      return payload.content;
    }
    const binary = atob(payload.content);
    return new TextDecoder().decode(
      Uint8Array.from(binary, (character) => character.charCodeAt(0)),
    );
  }
}

export class Sandbox {
  readonly connection: HarborboxConnection;
  readonly commands: Commands;
  readonly files: Files;
  sandboxId: string;
  status: SandboxStatus;

  private constructor(
    connection: HarborboxConnection,
    payload: SandboxPayload,
  ) {
    this.connection = connection;
    this.sandboxId = payload.id;
    this.status = payload.status;
    this.commands = new Commands(this);
    this.files = new Files(this);
  }

  static async create(
    template: string,
    options: SandboxCreateOptions = {},
  ): Promise<Sandbox> {
    if (!template) {
      throw new Error("A registered Harborbox template is required");
    }
    const connection = new HarborboxConnection(options);
    const timeoutMs = options.timeoutMs ?? 20 * 60_000;
    const metadata = { ...(options.metadata ?? {}) };
    const payload = await connection.request<SandboxPayload>(
      "/v1/sandboxes",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          template,
          memory_mb: options.memoryMb ?? null,
          cpu: options.cpu ?? null,
          idle_timeout_seconds: Math.max(0, Math.ceil(timeoutMs / 1000)),
          metadata,
        }),
      },
    );
    return new Sandbox(connection, payload);
  }

  async isRunning(): Promise<boolean> {
    const payload = await this.connection.request<SandboxPayload>(
      `/v1/sandboxes/${this.sandboxId}`,
    );
    this.status = payload.status;
    return this.status !== "killed" && this.status !== "failed";
  }

  async ensureReady(): Promise<void> {
    const payload = await this.connection.request<SandboxPayload>(
      `/v1/sandboxes/${this.sandboxId}`,
    );
    this.status = payload.status;
    if (this.status === "running") {
      return;
    }
    if (this.status === "killed" || this.status === "failed") {
      throw new Error(`sandbox is ${this.status}`);
    }
    const warmup = await this.commands.run("true", { timeoutMs: 30_000 });
    if (warmup.exitCode !== 0) {
      throw new Error(warmup.error?.value ?? "sandbox warmup failed");
    }
    this.status = "running";
  }

  async setTimeout(timeoutMs: number): Promise<void> {
    if (timeoutMs < 0) {
      throw new Error("timeoutMs must be non-negative");
    }
    const payload = await this.connection.request<SandboxPayload>(
      `/v1/sandboxes/${this.sandboxId}`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          idle_timeout_seconds: Math.ceil(timeoutMs / 1000),
        }),
      },
    );
    this.status = payload.status;
  }

  async kill(): Promise<void> {
    await this.connection.request<void>(
      `/v1/sandboxes/${this.sandboxId}`,
      { method: "DELETE" },
    );
    this.status = "killed";
  }
}
