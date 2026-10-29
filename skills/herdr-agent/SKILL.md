---
name: herdr-agent
description: Use when the user asks to hand a task to a fresh agent in its own Herdr tab ("spin up an agent for X", "/herdr-agent"), or asks about, closes, or resumes agents launched this way ("how's the auth agent doing", "close the finished agents", "resume the export one"). Launches `claude --worktree` in a new named tab and keeps a SQLite record of every agent.
---

# herdr-agent

Runs a task in a new Claude agent: a new Herdr tab, its own `claude --worktree`, the user's instructions sent as the first prompt. Every agent is recorded in `~/Developer/.herdr-agents.db` so any later session can answer questions about it, close it, or resume it.

All operations go through one script. Run it directly (its shebang uses `uv run`):

```bash
<skill base directory>/scripts/herdr_agent.py <command> ...
```

Every command prints JSON. A failure prints `{"error": ...}` and exits 1; relay the error, don't work around it with raw `herdr` or `git` commands.

## Launch

1. **Pick the target.** The default is the repo of the current working directory, with the tab in the current Herdr workspace (`--here`). If the user names a repo, use `--repo <path>` instead, and the script finds the Herdr workspace whose panes sit in that repo, or creates one named after the repo. Pass `--workspace <id>` only when the user names one or the script reports an ambiguous match (then ask the user which one).
2. **Choose a slug.** 2–4 words, kebab-case, describing the task (`fix-login-redirect`). It must match `[a-z][a-z0-9_-]{0,31}`. Never a bare ID: for a GitHub issue or bead, read it and name what it does (`issue-142` → `fix-csv-export-dates`). It becomes the tab name, the worktree name (`.claude/worktrees/<slug>`), the Herdr agent name and the record key.
3. **Write the prompt file.** Put the user's instructions **verbatim** in a temporary file, preceded by these context lines:
   ```
   Context: dispatched from another Claude session via herdr-agent; you are in your own worktree.
   If this task is complex enough to warrant it (multi-step, design decisions, non-trivial bugs), use the superpowers skills for it. For any UI/UX work, use the impeccable skill.
   ```
   Add nothing else: no plans, no rewording.
4. **Run:**
   ```bash
   herdr_agent.py launch <slug> --here --prompt-file <file>
   herdr_agent.py launch <slug> --repo ~/Developer/<repo> --prompt-file <file>
   ```
5. **Report** the tab name, workspace, and branch in one or two lines. Don't wait for or watch the agent; the user's focus stays where it is.

If launch reports that the repo isn't trusted, tell the user to run `claude` once in that repo and accept the trust dialog. Don't accept it for them.

## Questions about agents

- `herdr_agent.py list` shows the running agents; `list --all` includes closed ones; `--repo <path>` filters by repo. `live_status` comes from Herdr: `working`, `blocked` (waiting on an approval or question), `idle`/`done` (finished its turn), or null when it's not running.
- `herdr_agent.py show <slug>` gives the full record plus `last_message`, the agent's latest reply taken from its session transcript. That's usually the answer to "how's X going?". For more detail, read the `transcript` JSONL path it returns.
- Match the user's loose references ("the auth one") against slugs and instructions. If two agents fit, ask which one.
- Don't poll or monitor agents unless asked.

## Close

`herdr_agent.py close <slug>` closes the tab and marks the agent closed. Then:
- If the branch is provably merged (contained in the default branch locally or on origin, or a merged GitHub PR exists) and the worktree is clean, it removes the worktree and branch. The agent can no longer be resumed.
- Otherwise it keeps the worktree, and the agent stays resumable. Running `close` again on an already-closed agent re-checks the merge, so that's how to clean up after the branch lands later.
- If it was the last tab in its workspace, Herdr closes the workspace too.

If the agent is `working` or `blocked`, close refuses. Tell the user, and pass `--force` only after they confirm. For "close the finished ones", close the agents whose `live_status` is `idle` or `done`, and report each result line.

## Resume

`herdr_agent.py resume <slug> [--prompt-file <file>]` opens a new tab in the repo's workspace inside the kept worktree, runs `claude --resume <session-id>` (full conversation restored), and marks it running again. Use `--prompt-file` when the user gives follow-up instructions.

An agent whose tab was closed outside this script is marked closed automatically the next time the record is read, and it is still resumable.
