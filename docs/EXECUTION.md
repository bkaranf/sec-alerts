# Execution plan and model verification

1. Build the standalone Python/uv foundation and stable worker interfaces.
2. Collect real TFC/PFSI disclosures; generate and inspect evidence, briefing, and original MIME attachments.
3. Add WFC/RKT through the same pipeline, verify expansion candidate identities and limitations.
4. Test persistence, bounded acquisition, email failure handling and Windows scheduling; leave recurring sending disabled.
5. Separate Luna review of integrated code and actual reports, root inspection and fixes, final tested handoff.

Model verification on 2026-09-04: local `~/.codex/config.toml` selects `gpt-6-astra` with `ultra`; model cache lists Astra low/medium/high/xhigh/max/ultra and `gpt-5.6-luna` low/medium/high/xhigh/max. The session exposes collaboration.spawn_agent with explicit `model`, `reasoning_effort` and `fork_turns` controls. Workers are requested with `model=gpt-5.6-luna`, `reasoning_effort=max`, `fork_turns=none`; configuration is not simulated in prompts. Actual server internals are not independently attestable beyond the supported runtime's routing metadata.

Official sources reviewed:
- https://developers.openai.com/api/docs/models/gpt-5.6-luna
- https://developers.openai.com/api/docs/models/gpt-6-astra
- https://learn.chatgpt.com/docs/agent-configuration/subagents

No preexisting code or instructions were present in this repository; no commits, push, deployment, or scheduling activation is part of this build. Live SEC identity: configured=yes (value never reported). Optional AI key and SMTP password: configured=no at foundation inspection.
# Current objective

The active product and acceptance requirements are in [GOAL.md](../GOAL.md). Each detected company earnings release now produces a separate draft. Astra leads the email design and UI work and reviews actual phone and desktop renders. Luna implements precise Astra template instructions and supports source, evidence, reliability, and factual review.
