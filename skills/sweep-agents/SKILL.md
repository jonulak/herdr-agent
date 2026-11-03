---
name: sweep-agents
description: Use when the user asks to clean up herdr-agents in bulk ("sweep the agents", "close everything that's finished", "clean up the herdr tabs", "/sweep-agents"). Checks every open agent, reports which are done, merged if they needed merging, have nothing left to do, and lose nothing when their worktree is removed, then closes the ones the user confirms.
---

# sweep-agents

Finds every open herdr-agent that can go without losing anything, shows the user a report, and closes only what they confirm. It uses the herdr-agent script (`../herdr-agent/scripts/herdr_agent.py`, relative to this skill's base directory). Every command prints JSON.

## Steps

1. `herdr_agent.py list` to get the open agents.
2. For each one, `herdr_agent.py check <slug>`. It changes nothing and returns `busy`, `worktree_removable`, `worktree` (the reason), `instructions` and `last_message`.
3. An agent is **closable** only if **all** of these hold:
   - **Done:** `busy` is false (`live_status` is `idle`, `done` or null).
   - **Merged, and nothing to lose:** `worktree_removable` is true. That means the worktree has no uncommitted or untracked files, and the branch is in the default branch (locally or on origin) or is the head of a merged PR. A branch with no commits of its own counts as merged, so a discussion-only agent passes.
   - **Nothing left:** you read `last_message` against `instructions` and judge the task finished. It fails if the agent:
     - asked a question or is waiting on a decision, or is presenting a plan for approval;
     - mentions an open PR, failing checks, review feedback to handle, or follow-up work it said it would do;
     - mentions work kept outside its own branch that isn't merged: a scratch or temp file, another branch it pushed, a stash;
     - stopped partway through, or `last_message` is null.

     When unsure, it fails.
4. **Report, then stop.** Close nothing yet. Show this report, always in this shape:

   ```markdown
   **Would close**

   | Agent | Why it passes |
   |---|---|
   | `fix-install-test-port` | PR #117 merged, issue #115 closed. Only promotion to `main` is left, and that's already in PR #118. |

   **Would keep**

   | Agent | Reason |
   |---|---|
   | `verify-clinical-claims` | Still working ("applying urticaria + acne corrections"), uncommitted changes. |
   | `fix-pg-collation-mismatch` | Merge check passes, but its work isn't finished: it says it will merge promotion PR #118 and then run the reindex. |
   ```

   - **Why it passes:** name the merge evidence (PR number or merge into the default branch) and, from `last_message`, why nothing is left.
   - **Reason:** name the criterion that failed, quoting or citing the agent where that helps (the open PR, the question it asked, the file left outside the repo).
   - After the tables, flag any **judgment call** with ⚠️: an agent you put in a table only after reading between the lines, such as a leftover branch that another agent's message says it already handled. Say what a strict reading of the criteria would do, and what you checked (for example `git ls-remote` showed the branch gone).
   - If there's nothing to close, say so and stop.

   End by asking which agents to close: all of them under "Would close", some, or none.
5. **Wait for the user's answer.** Close only agents they confirm. If they name one under "Would keep", it's a normal `close` (plus `--force` if busy).
6. Before each close, run `check <slug>` again. If its result changed since the report (it's busy again, or `worktree_removable` is now false), skip it and say why. Otherwise `herdr_agent.py close <slug>`. If the close result doesn't say `removed worktree`, report it.
7. Report one line per agent closed or skipped.

Never close an agent without the user's go-ahead in this run, and never clean up a worktree by hand with `git`.
