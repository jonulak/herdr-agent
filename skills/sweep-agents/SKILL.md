---
name: sweep-agents
description: Use when the user asks to clean up herdr-agents in bulk ("sweep the agents", "close everything that's finished", "clean up the herdr tabs", "/sweep-agents"). Checks every open agent and closes only those that are done, merged if they needed merging, have nothing left to do, and lose nothing when their worktree is removed.
---

# sweep-agents

Closes every open herdr-agent that can go without losing anything, and leaves the rest alone. It uses the herdr-agent script (`../herdr-agent/scripts/herdr_agent.py`, relative to this skill's base directory). Every command prints JSON.

## Steps

1. `herdr_agent.py list` to get the open agents.
2. For each one, `herdr_agent.py check <slug>`. It changes nothing and returns `busy`, `worktree_removable`, `worktree` (the reason), `instructions` and `last_message`.
3. Close an agent only if **all** of these hold:
   - **Done:** `busy` is false (`live_status` is `idle`, `done` or null).
   - **Merged, and nothing to lose:** `worktree_removable` is true. That means the worktree has no uncommitted or untracked files, and the branch is in the default branch (locally or on origin) or is the head of a merged PR. A branch with no commits of its own counts as merged, so a discussion-only agent passes.
   - **Nothing left:** you read `last_message` against `instructions` and judge the task finished. It fails if the agent:
     - asked a question or is waiting on a decision, or is presenting a plan for approval;
     - mentions an open PR, failing checks, review feedback to handle, or follow-up work it said it would do;
     - mentions work kept outside its own branch that isn't merged: a scratch or temp file, another branch it pushed, a stash;
     - stopped partway through, or `last_message` is null.

     When unsure, it fails.
4. For each agent that passes, `herdr_agent.py close <slug>`. Don't pass `--force`. If the close result doesn't say `removed worktree`, report it, because something changed between check and close.
5. Report one line per agent: `closed`, or `kept` plus the criterion that failed (for example "kept: PR #112 still open", "kept: asked whether to also update the docs", "kept: uncommitted changes").

Never close an agent that fails a criterion, and never clean up a worktree by hand with `git`. If the user wants a kept agent gone anyway, that's a normal `close` (plus `--force` if busy), after they confirm.
