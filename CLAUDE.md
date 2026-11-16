@AGENTS.md

## Claude Code
- Delegating to subagents: give each one explicit file ownership. Parallel agents must never edit the same files,
  and docs/API.md is the contract between the server and web agents. Give new agents distinct names.
- Browser smoke tests: use the Playwright MCP tools (their own headless browser) in a tab of your own; never the
  Claude in Chrome tools. Playwright output lands in `.playwright-mcp/` (gitignored); delete it when done.
- Put temporary files (scripts, renders, reports) in the session scratchpad, not in the repository. Leave no
  plans, handoffs, audits or other artifacts in the checkout (see "Leave the checkout clean" in `AGENTS.md`).
- Personal, machine-specific notes go in `CLAUDE.local.md` (gitignored), not here.
- Shared project rules live in `AGENTS.md`; changes to checks, conventions or safety belong there (or in both).
- Install Node 22+ for the full pytest check: it includes the dependency-free `tests/js/` regressions
  described in `AGENTS.md`, in addition to the separate JavaScript syntax check.
- Managed-AI tests require localhost binding, but no remote downloads or models. Frozen AI/app validation
  and the exact catalog/archive publication boundary are described in `AGENTS.md` and `packaging/`.
