/**
 * OpenCode plugin — Nebula L2 bootstrap + optional lightweight tools.
 *
 * Install (pick one):
 *   - copy to ~/.config/opencode/plugins/nebula-memory.js
 *   - or project .opencode/plugins/nebula-memory.js
 *   - or set "plugin": ["./path/to/nebula-memory.js"] in opencode.json
 *
 * Env (no lab inventory hard-coded beyond localhost default):
 *   NEBULA_BASE_URL           default http://127.0.0.1:26670
 *   NEBULA_BOOTSTRAP_PATH     default /v5/bootstrap
 *   NEBULA_ASK_PATH           default /ask
 *   NEBULA_ADD_PATH           default /memory/add
 *   NEBULA_BOOTSTRAP_BUDGET   default 800
 *   NEBULA_BOOTSTRAP_FOCUS    default session-start
 *   NEBULA_HOOK_TIMEOUT_MS    default 8000
 *   NEBULA_INJECT_SYSTEM      default 1  (0 to disable system inject)
 *   NEBULA_REGISTER_TOOLS     default 1  (0 to disable tool registration)
 *
 * Based on OpenCode plugin Hooks API (packages/plugin):
 *   - experimental.chat.system.transform  → budgeted bootstrap once per session
 *   - tool.*                              → optional ask/add (plain fetch)
 */

const DEFAULT_BASE = "http://127.0.0.1:26670"

function env(name, fallback) {
  const v = process.env[name]
  return v === undefined || v === "" ? fallback : v
}

function snip(s, n = 700) {
  const t = String(s || "").replace(/\s+/g, " ").trim()
  return t.length > n ? t.slice(0, n) + "…" : t
}

async function httpJson(url, body, timeoutMs) {
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeoutMs)
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify(body),
      signal: ctrl.signal,
    })
    const text = await res.text()
    let data
    try {
      data = text ? JSON.parse(text) : null
    } catch {
      data = { raw: text }
    }
    if (!res.ok) {
      const err = new Error(`HTTP ${res.status}`)
      err.data = data
      throw err
    }
    return data
  } finally {
    clearTimeout(timer)
  }
}

function extractBootstrap(data) {
  if (!data) return ""
  if (typeof data === "string") return data
  if (typeof data.bootstrap === "string") return data.bootstrap
  if (data.bootstrap && typeof data.bootstrap === "object") {
    return JSON.stringify(data.bootstrap)
  }
  if (typeof data.pack === "string") return data.pack
  if (typeof data.contract === "string") return data.contract
  // last resort: compact JSON, still snipped by caller
  return JSON.stringify(data)
}

/**
 * @param {import("@opencode-ai/plugin").PluginInput} [_ctx]
 */
export const NebulaMemoryPlugin = async (_ctx) => {
  const base = env("NEBULA_BASE_URL", DEFAULT_BASE).replace(/\/$/, "")
  const bootstrapPath = env("NEBULA_BOOTSTRAP_PATH", "/v5/bootstrap")
  const askPath = env("NEBULA_ASK_PATH", "/ask")
  const addPath = env("NEBULA_ADD_PATH", "/memory/add")
  const budget = Number(env("NEBULA_BOOTSTRAP_BUDGET", "800")) || 800
  const focusDefault = env("NEBULA_BOOTSTRAP_FOCUS", "session-start")
  const timeoutMs = Number(env("NEBULA_HOOK_TIMEOUT_MS", "8000")) || 8000
  const injectSystem = env("NEBULA_INJECT_SYSTEM", "1") !== "0"
  const registerTools = env("NEBULA_REGISTER_TOOLS", "1") !== "0"

  /** @type {Set<string>} */
  const injected = new Set()

  async function bootstrap(focus) {
    const url = `${base}${bootstrapPath.startsWith("/") ? bootstrapPath : `/${bootstrapPath}`}`
    const data = await httpJson(
      url,
      {
        focus: focus || focusDefault,
        budget,
        budget_chars: budget,
      },
      timeoutMs,
    )
    return snip(extractBootstrap(data), budget)
  }

  /** @type {Record<string, any>} */
  const hooks = {
    // Once per session: prepend budgeted L2 bootstrap into system prompt.
    "experimental.chat.system.transform": async (input, output) => {
      if (!injectSystem) return
      const sid = input?.sessionID || "default"
      if (injected.has(sid)) return
      injected.add(sid)
      try {
        const text = await bootstrap(focusDefault)
        if (!text) return
        output.system.push(
          [
            "[nebula-memory / L2 bootstrap]",
            text,
            "Rules: consume contract/bootstrap only; executable=false or superseded is not current policy;",
            "readback → re-open L1 authority; secrets never enter L2; on correction → supersede/writeback.",
          ].join("\n"),
        )
      } catch {
        // fail-open: never block chat
        output.system.push(
          "[nebula-memory] L2 bootstrap unavailable (fail-open). Still enforce recall-before-code + verify-before-assert.",
        )
      }
    },
  }

  if (registerTools) {
    // Minimal tool shapes without importing @opencode-ai/plugin (no install required).
    // OpenCode accepts tool definitions with description/args/execute.
    hooks.tool = {
      nebula_bootstrap: {
        description:
          "Fetch a budgeted L2 semantic-memory bootstrap for the current task focus. Prefer at task start.",
        args: {
          focus: {
            type: "string",
            description: "Task theme / focus string",
          },
          budget: {
            type: "number",
            description: "Character budget (optional)",
          },
        },
        async execute(args) {
          try {
            const b = args?.budget ? Number(args.budget) : budget
            const url = `${base}${bootstrapPath.startsWith("/") ? bootstrapPath : `/${bootstrapPath}`}`
            const data = await httpJson(
              url,
              {
                focus: args?.focus || focusDefault,
                budget: b,
                budget_chars: b,
              },
              timeoutMs,
            )
            return snip(extractBootstrap(data), b)
          } catch (e) {
            return `nebula_bootstrap failed (fail-open): ${e?.message || e}`
          }
        },
      },
      nebula_ask: {
        description:
          "Query L2 semantic memory. Returns compact contract JSON. Do not dump raw hits into context.",
        args: {
          query: {
            type: "string",
            description: "Natural language question",
          },
          top_k: {
            type: "number",
            description: "Optional top_k (default 5)",
          },
        },
        async execute(args) {
          try {
            const url = `${base}${askPath.startsWith("/") ? askPath : `/${askPath}`}`
            const data = await httpJson(
              url,
              {
                query: args?.query || "",
                top_k: args?.top_k ?? 5,
              },
              timeoutMs,
            )
            return snip(JSON.stringify(data), 4000)
          } catch (e) {
            return `nebula_ask failed: ${e?.message || e}`
          }
        },
      },
      nebula_add: {
        description:
          "Write a self-contained lesson/decision into L2. Rejects secret-like content server-side when implemented.",
        args: {
          content: { type: "string", description: "Self-contained memory text" },
          category: { type: "string", description: "lesson|decision|infrastructure|project|sop" },
          importance: { type: "number", description: "0..1" },
        },
        async execute(args) {
          try {
            const url = `${base}${addPath.startsWith("/") ? addPath : `/${addPath}`}`
            const data = await httpJson(
              url,
              {
                content: args?.content || "",
                category: args?.category || "lesson",
                importance: args?.importance ?? 0.6,
              },
              timeoutMs,
            )
            return snip(JSON.stringify(data), 2000)
          } catch (e) {
            return `nebula_add failed: ${e?.message || e}`
          }
        },
      },
    }
  }

  return hooks
}

// OpenCode loads all named exports that look like plugins.
export default NebulaMemoryPlugin
