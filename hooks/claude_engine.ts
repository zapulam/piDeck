// piDeck forwards numeric engine measurements without changing prompts, tools, or permissions.
import type { EngineInterface, Register, SessionUsage } from 'claude-code'

const command: readonly string[] = __COMMAND__

async function publish($: EngineInterface, fields: Record<string, unknown>): Promise<void> {
  try {
    await $.process.run(command, {
      stdin: JSON.stringify({ session_id: await $.session.id(), ...fields }), timeoutMs: 2500,
    })
  } catch {
    // The Python helper queues delivery. piDeck must never interrupt the harness.
  }
}

async function measure($: EngineInterface, data: Pick<SessionUsage, 'context' | 'rateLimits'>): Promise<void> {
  const window = (kind: string, minutes: number) => {
    const limit = data.rateLimits.find(value => value.kind === kind)
    if (limit === undefined) return null
    const reset = limit.resetsAt === undefined ? undefined : Date.parse(limit.resetsAt) / 1000
    return {
      used_pct: Math.max(0, Math.min(100, limit.percentUsed)), window_minutes: minutes,
      ...(Number.isFinite(reset) ? { resets_at: reset } : {}),
    }
  }
  const primary = window('five_hour', 300), secondary = window('seven_day', 10080)
  await publish($, {
    event: 'UsageUpdate',
    ...(data.context.percent === undefined ? {} : { context_pct: data.context.percent }),
    ...(primary === null && secondary === null ? {} : { usage: { primary, secondary } }),
  })
}

export const register: Register = on => {
  on('session.start', async ($, event, next) => {
    try { await measure($, await $.session.usage()) } catch { /* Optional observations never block a session. */ }
    return next(event)
  })
  on('session.measure', async ($, event, next) => {
    try { await measure($, event) } catch { /* Unsupported measurements remain unavailable. */ }
    return next(event)
  })
}
