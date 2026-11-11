// Project-local policy plugin. Uses public agent/created and tools APIs.
// No model requests, credential access, or tool implementations.
export const name = 'video-translation-no-tools';
export const inject = ['tools', 'agents'];

export function apply(ctx) {
  // A monotonic global guard also rejects calls fabricated by a provider.
  ctx.tools.guard(() => 'VT_HARNESS_TOOLS_DISABLED');
  ctx.on('agent/created', ({ agent }) => {
    agent.ctx.tools.restrict({ allow: [] });
    if (agent.ctx.tools.schemas(agent).length !== 0) {
      throw new Error('VT_HARNESS_NONEMPTY_TOOL_ROSTER');
    }
  });
}
