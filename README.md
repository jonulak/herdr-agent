# herdr-agent

A Claude Code plugin that hands a task to a fresh Claude agent in its own [Herdr](https://herdr.dev) tab, then keeps track of it.

- **Launch:** creates a tab named after the task, in the repo's Herdr workspace (or a new workspace if the repo has none). Starts `claude --worktree <slug>` there and sends your instructions word for word.
- **Track:** records every agent in a SQLite registry (`~/Developer/.herdr-agents.db`, override with `HERDR_AGENT_DB`), so any later session can answer "how's the X agent doing?" from the agent's latest reply.
- **Close:** closes the tab. It removes the worktree and branch only when it can prove the branch was merged.
- **Resume:** opens a new tab in the kept worktree and runs `claude --resume <session-id>`, restoring the full conversation.

## Install

```bash
claude plugin marketplace add jonulak/herdr-agent
claude plugin install herdr-agent@herdr-agent --scope user
```

Requires `herdr` with the session running inside Herdr (`HERDR_ENV=1`), plus `git`, `uv` and, optionally, `gh` for detecting merged PRs.

## Use

Type `/herdr-agent <task>` in any Claude Code session, or ask for an agent in plain words. The script lives at `skills/herdr-agent/scripts/herdr_agent.py` and has these commands: `launch`, `list`, `show`, `close`, `resume`.

A repo has to be trusted by Claude Code (run `claude` there once) before `--worktree` will start in it.
