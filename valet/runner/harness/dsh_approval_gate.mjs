// Valet's approval gate for DeepSeek Harness: a dsh (Cordis) plugin. The runner writes it into the agent's
// read-only config dir and mounts it from the patch it writes (valet/runner/harness/deepseek.py).
//
// dsh asks for approval only when a sandboxed command wants out of its sandbox. In a container its sandbox is off
// (it can't confine there), so without this gate nothing would ever ask. The gate carries the agent's permission
// level for every tool call whose tool isn't in `config.allow` (the read-only and bookkeeping tools) and doesn't
// start with one of `config.allowPrefixes` (`mcp__valet__`: the MCP gateway's tools, which the gateway decides):
// `config.mode` "deny" (locked) refuses it, "ask" asks the way Claude Code asks before edits and commands, "pass"
// (trusted, unrestricted) lets it run. An unknown mode asks. Questions go through dsh's approval service, which its
// ACP server sends to Valet as session/request_permission. Any other gate's deny, cancel or ask still wins.
//
// The level also decides dsh's own permission preset (sandbox + approval policy; Valet's patch sets the default).
// dsh pins a preset on a session when it starts and restores it on resume, so a level changed since would not reach
// a resumed session: one begun `trusted` (approval "never") and resumed at `ask` would reject the gate's questions
// unasked, and a host session begun `unrestricted` would keep full access. So every top-level session, new or
// resumed, is put on the configured default preset. Subagents keep the policy their delegation pinned.
//
// Fail closed: the gate publishes the `valetApprovalGate` service once its listener is on, and Valet's patch makes
// the ACP server wait for that service. A gate that doesn't load means no ACP server, so the agent fails to start
// instead of running without approvals (dsh itself only warns about a plugin that didn't activate).
export const name = 'valet-approval'
export const inject = ['tools', 'permissionPresets']
export const SERVICE = 'valetApprovalGate'

// PTC mode's transport tool: its inner calls are gated one by one, so the wrapper itself isn't.
const RUN_CODE = 'run_code'

export function apply(ctx, config = {}) {
  const allow = new Set(config.allow ?? [])
  const prefixes = config.allowPrefixes ?? []
  const mode = config.mode ?? 'ask'
  ctx.on('tools/pre-execute', async (exec, next) => {
    const downstream = await next()
    if (downstream.kind !== 'allow') return downstream
    if (allow.has(exec.name) || (exec.parent === undefined && exec.name === RUN_CODE)) return downstream
    if (prefixes.some((p) => exec.name.startsWith(p))) return downstream
    if (mode === 'pass') return downstream
    if (mode === 'deny') return { kind: 'deny', reason: `this agent is locked: Valet doesn't let it use "${exec.name}"` }
    return { kind: 'ask', reason: `Valet asks before "${exec.name}"` }
  })
  ctx.on('session/created', (session) => {
    if (session.header?.origin === 'subagent' || session.header?.parentSession !== undefined) return
    ctx.permissionPresets.set(session, ctx.permissionPresets.defaultPreset)
  })
  ctx.provide(SERVICE, { allow: [...allow], allowPrefixes: [...prefixes], mode })
}
