/**
 * stdio bootstrap for `claw-squad mcp`.
 *
 * Dynamic-imports `@modelcontextprotocol/sdk` (declared in
 * `optionalDependencies`) so the default install stays lean — only
 * `claw-squad mcp` users pay for it. Mirrors the OTel pattern in
 * `src/tracing.ts`.
 *
 * Exposes the read-only tools defined in `mcp/handlers.ts`:
 *   - claw_squad_dashboard
 *   - claw_squad_dashboard_diff
 *   - claw_squad_runs_list
 *   - claw_squad_runs_purge
 *
 * The full `claw_squad_run` (interactive + multi-hour orchestrator
 * dispatch) is intentionally out-of-scope for this PR; it deserves
 * dedicated streaming-notification work.
 */

import { MCP_TOOLS, type ToolDescriptor } from "./handlers.js";
import { VERSION } from "../version.js";

/**
 * Result of `tools/call` after a handler runs. The MCP SDK expects
 * a `{ content: [{ type, text }] }` shape; we serialise the handler's
 * return value as JSON so a downstream agent can JSON.parse it.
 */
function _wrap(result: Record<string, unknown>): {
  content: Array<{ type: "text"; text: string }>;
} {
  return {
    content: [
      {
        type: "text",
        text: JSON.stringify(result, null, 2),
      },
    ],
  };
}

function _wrapError(err: unknown): {
  content: Array<{ type: "text"; text: string }>;
  isError: true;
} {
  const msg = err instanceof Error ? err.message : String(err);
  return {
    content: [{ type: "text", text: `error: ${msg}` }],
    isError: true,
  };
}

/**
 * Boot the MCP server over stdio. Returns a promise that resolves
 * when the transport closes (i.e. the parent process disconnects).
 *
 * Throws a clear, actionable error when the SDK isn't installed —
 * the user opted into this path by running `claw-squad mcp`, so an
 * "install the optional dep" message is the right UX.
 */
export async function runMcpServer(): Promise<void> {
  // SDK paths are typed loosely as `any` because the package lives in
  // `optionalDependencies` — environments that opted out won't have
  // them resolvable. We dynamic-import inside a try/catch so the
  // unconfigured path produces an actionable error message.
  let sdkServer: any;
  let sdkStdio: any;
  let sdkTypes: any;
  try {
    sdkServer = await import("@modelcontextprotocol/sdk/server/index.js");
    sdkStdio = await import("@modelcontextprotocol/sdk/server/stdio.js");
    sdkTypes = await import("@modelcontextprotocol/sdk/types.js");
  } catch (cause) {
    throw new Error(
      "claw-squad mcp requires the @modelcontextprotocol/sdk package.\n" +
        "  Install it: pnpm add @modelcontextprotocol/sdk\n" +
        "  Or:         npm install @modelcontextprotocol/sdk\n" +
        `  (underlying error: ${(cause as Error)?.message ?? cause})`,
    );
  }

  const { Server } = sdkServer;
  const { StdioServerTransport } = sdkStdio;
  const { ListToolsRequestSchema, CallToolRequestSchema } = sdkTypes;

  const server = new Server(
    { name: "claw-squad", version: VERSION },
    { capabilities: { tools: {} } },
  );

  // Register the tool catalog. The handler dispatch runs the pure
  // function from handlers.ts; protocol concerns stay here.
  server.setRequestHandler(ListToolsRequestSchema, async () => {
    return {
      tools: MCP_TOOLS.map((t: ToolDescriptor) => ({
        name: t.name,
        description: t.description,
        inputSchema: t.inputSchema,
      })),
    };
  });

  server.setRequestHandler(CallToolRequestSchema, async (req: any) => {
    const name = req.params.name as string;
    const args = (req.params.arguments ?? {}) as Record<string, unknown>;
    const tool = MCP_TOOLS.find((t) => t.name === name);
    if (!tool) {
      return _wrapError(`unknown tool: ${name}`);
    }
    try {
      const result = tool.handler(args);
      return _wrap(result);
    } catch (err) {
      // Caught here so a bad-arg error doesn't tear down the server
      // and disconnect the MCP client — they get one error response
      // and can retry with corrected arguments.
      return _wrapError(err);
    }
  });

  const transport = new StdioServerTransport();
  await server.connect(transport);
  // server.connect() returns once the transport is wired. Hold the
  // process alive until the parent disconnects (StdioServerTransport
  // will resolve its onClose when stdin closes). The Server's
  // .onclose hook is the standard way to await termination.
  await new Promise<void>((resolve) => {
    server.onclose = () => resolve();
  });
}
