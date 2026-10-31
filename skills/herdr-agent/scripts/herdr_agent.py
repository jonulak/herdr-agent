#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""Launch, track, close, and resume Claude agents in Herdr tabs.

Each agent runs `claude --worktree <slug>` in its own Herdr tab, and is
recorded in a SQLite registry so later sessions can answer questions about it.
All output is JSON on stdout; failures exit non-zero with {"error": ...}.
"""

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

DB_PATH = Path(os.environ.get("HERDR_AGENT_DB", Path.home() / "Developer" / ".herdr-agents.db"))
SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
START_TIMEOUT_MS = "90000"
DEFAULT_MODEL = "opus"

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    slug                TEXT PRIMARY KEY,
    repo_path           TEXT NOT NULL,
    workspace_id        TEXT,
    tab_id              TEXT,
    pane_id             TEXT,
    worktree_path       TEXT,
    branch              TEXT,
    session_id          TEXT NOT NULL,
    instructions        TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'running',  -- running | closed
    created_at          TEXT NOT NULL,
    closed_at           TEXT,
    close_note          TEXT,
    worktree_removed_at TEXT,
    resume_count        INTEGER NOT NULL DEFAULT 0,
    last_resumed_at     TEXT,
    model               TEXT
);
"""


class Fail(Exception):
    pass


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    if "model" not in {r[1] for r in conn.execute("PRAGMA table_info(agents)")}:
        conn.execute("ALTER TABLE agents ADD COLUMN model TEXT")
    return conn


def run(*cmd: str, cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise Fail(f"{' '.join(cmd[:3])}... failed: {(p.stderr or p.stdout).strip()}")
    return p


def herdr(*args: str) -> dict:
    return json.loads(run("herdr", *args).stdout)["result"]


def git(repo: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return run("git", "-C", repo, *args, check=check)


# --- resolution -------------------------------------------------------------


def repo_root(path: str) -> str:
    """Main checkout root for any path inside a repo or one of its worktrees."""
    if git(path, "rev-parse", "--git-dir", check=False).returncode != 0:
        raise Fail(f"{path} is not inside a git repository; pass --repo")
    common =git(path, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    return str(Path(common).parent) if common.endswith("/.git") else git(path, "rev-parse", "--show-toplevel").stdout.strip()


def ensure_trusted(repo: str) -> None:
    try:
        projects = json.loads((Path.home() / ".claude.json").read_text()).get("projects", {})
    except (OSError, json.JSONDecodeError):
        return
    if not projects.get(repo, {}).get("hasTrustDialogAccepted"):
        raise Fail(f"{repo} is not trusted by Claude Code yet, so --worktree will refuse to start. "
                   f"Ask the user to run `claude` once in {repo} and accept the trust dialog.")


def live_agents() -> list[dict]:
    return herdr("agent", "list")["agents"]


def live_agent(row: sqlite3.Row) -> dict | None:
    for a in live_agents():
        if a.get("pane_id") == row["pane_id"] and a.get("name") == row["slug"]:
            return a
    return None


def resolve_workspace(repo: str) -> dict:
    """Pick the Herdr workspace whose panes sit in this repo (or its worktrees).

    Returns {"workspace_id": ...}, {"workspace_id": None} when none exist, or
    raises with the candidates when the choice is ambiguous.
    """
    workspaces = herdr("workspace", "list")["workspaces"]
    scores = []
    for ws in workspaces:
        panes = herdr("pane", "list", "--workspace", ws["workspace_id"])["panes"]
        hits = sum(
            1 for p in panes
            if any(c == repo or c.startswith(repo + "/") for c in (p.get("cwd"), p.get("foreground_cwd")) if c)
        )
        if hits:
            label_match = ws.get("label", "").lower() == Path(repo).name.lower()
            scores.append((label_match, hits, ws))
    if not scores:
        return {"workspace_id": None}
    scores.sort(key=lambda s: (s[0], s[1]), reverse=True)
    if len(scores) > 1 and scores[0][:2] == scores[1][:2]:
        raise Fail("ambiguous workspace for " + repo + "; pass --workspace. Candidates: " +
                   ", ".join(f'{s[2]["workspace_id"]} ({s[2]["label"]}, {s[1]} panes)' for s in scores))
    return {"workspace_id": scores[0][2]["workspace_id"], "label": scores[0][2]["label"]}


def open_tab(repo: str, cwd: str, slug: str, workspace_id: str | None) -> tuple[str, str, str]:
    """Create a tab named `slug` (or a new workspace if none). Returns (ws, tab, pane)."""
    if not workspace_id:
        r = herdr("workspace", "create", "--cwd", cwd, "--label", Path(repo).name, "--no-focus")
        herdr("tab", "rename", r["tab"]["tab_id"], slug)
        return r["workspace"]["workspace_id"], r["tab"]["tab_id"], r["root_pane"]["pane_id"]
    r = herdr("tab", "create", "--workspace", workspace_id, "--cwd", cwd, "--label", slug, "--no-focus")
    return workspace_id, r["tab"]["tab_id"], r["root_pane"]["pane_id"]


def worktree_branch(repo: str, path: str) -> str | None:
    out = git(repo, "worktree", "list", "--porcelain").stdout
    for block in out.split("\n\n"):
        lines = block.splitlines()
        if lines and lines[0] == f"worktree {path}":
            for line in lines:
                if line.startswith("branch refs/heads/"):
                    return line.removeprefix("branch refs/heads/")
    return None


def get_row(conn: sqlite3.Connection, slug: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM agents WHERE slug = ?", (slug,)).fetchone()
    if not row:
        raise Fail(f"no agent named {slug} in the registry")
    return row


def transcript(row: sqlite3.Row) -> Path | None:
    hits = list((Path.home() / ".claude" / "projects").glob(f"*/{row['session_id']}.jsonl"))
    return hits[0] if hits else None


def last_message(row: sqlite3.Row) -> str | None:
    path = transcript(row)
    if not path:
        return None
    last = None
    for line in path.read_text().splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") == "assistant":
            texts = [c["text"] for c in d.get("message", {}).get("content", []) if c.get("type") == "text"]
            if texts:
                last = "\n".join(texts)
    return last


# --- merge detection --------------------------------------------------------


def default_branch(repo: str) -> str:
    p = git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False)
    if p.returncode == 0:
        return p.stdout.strip().removeprefix("origin/")
    for b in ("main", "master"):
        if git(repo, "rev-parse", "--verify", "--quiet", b, check=False).returncode == 0:
            return b
    raise Fail("could not determine the default branch")


def merge_evidence(repo: str, branch: str) -> str | None:
    """A human-readable reason the branch is merged, or None if not provable."""
    base = default_branch(repo)
    if git(repo, "merge-base", "--is-ancestor", branch, base, check=False).returncode == 0:
        return f"{branch} is contained in local {base}"
    if git(repo, "remote", check=False).stdout.strip():
        git(repo, "fetch", "--quiet", "origin", base, check=False)
        if git(repo, "merge-base", "--is-ancestor", branch, f"origin/{base}", check=False).returncode == 0:
            return f"{branch} is contained in origin/{base}"
        p = run("gh", "pr", "list", "--head", branch, "--state", "merged", "--json", "number,url",
                cwd=repo, check=False)
        if p.returncode == 0 and json.loads(p.stdout or "[]"):
            pr = json.loads(p.stdout)[0]
            return f"PR #{pr['number']} from {branch} was merged ({pr['url']})"
    return None


# --- commands ---------------------------------------------------------------


def submit_prompt(slug: str, pane_id: str, text: str) -> None:
    """Send the prompt and make sure the agent starts on it.

    The typed text sometimes lands without its Enter; herdr reports that as a
    stall, and pressing Enter again submits what is already in the input box.
    """
    p = run("herdr", "agent", "prompt", slug, text, "--wait", "--until", "working", "--until", "blocked",
            "--timeout", "15000", check=False)
    if p.returncode == 0:
        return
    if "agent_prompt_stalled" not in p.stdout + p.stderr:
        raise Fail(f"herdr agent prompt failed: {(p.stderr or p.stdout).strip()}")
    for _ in range(3):
        run("herdr", "pane", "send-keys", pane_id, "Enter")
        if run("herdr", "agent", "wait", slug, "--until", "working", "--until", "blocked",
               "--timeout", "5000", check=False).returncode == 0:
            return
    raise Fail(f"{slug} never started on its prompt; check its tab ({pane_id})")


def cmd_launch(a) -> dict:
    if not SLUG_RE.match(a.slug):
        raise Fail(f"slug {a.slug!r} must match {SLUG_RE.pattern}")
    instructions = Path(a.prompt_file).read_text()
    if not instructions.strip():
        raise Fail("prompt file is empty")
    conn = db()
    if conn.execute("SELECT 1 FROM agents WHERE slug = ?", (a.slug,)).fetchone():
        raise Fail(f"slug {a.slug} is already in the registry; pick another")
    if any(x.get("name") == a.slug for x in live_agents()):
        raise Fail(f"a live Herdr agent is already named {a.slug}; pick another")

    if a.repo:
        repo = repo_root(a.repo)
    elif a.here:
        repo = repo_root(os.getcwd())
    else:
        raise Fail("pass --repo PATH or --here")
    worktree = f"{repo}/.claude/worktrees/{a.slug}"
    if Path(worktree).exists():
        raise Fail(f"{worktree} already exists; pick another slug")
    ensure_trusted(repo)

    if a.workspace:
        ws_id = a.workspace
    elif a.here and os.environ.get("HERDR_WORKSPACE_ID"):
        ws_id = os.environ["HERDR_WORKSPACE_ID"]
    else:
        ws_id = resolve_workspace(repo)["workspace_id"]
    ws_id, tab_id, pane_id = open_tab(repo, repo, a.slug, ws_id)

    session_id = str(uuid.uuid4())
    run("herdr", "agent", "start", a.slug, "--kind", "claude", "--pane", pane_id, "--timeout", START_TIMEOUT_MS,
        "--", "--worktree", a.slug, "--session-id", session_id, "--name", a.slug, "--model", a.model)
    branch = worktree_branch(repo, worktree)
    conn.execute(
        "INSERT INTO agents (slug, repo_path, workspace_id, tab_id, pane_id, worktree_path, branch, session_id,"
        " instructions, created_at, model) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (a.slug, repo, ws_id, tab_id, pane_id, worktree, branch, session_id, instructions, now(), a.model),
    )
    conn.commit()
    submit_prompt(a.slug, pane_id, instructions)
    return {"launched": a.slug, "repo": repo, "workspace_id": ws_id, "tab_id": tab_id, "pane_id": pane_id,
            "worktree": worktree, "branch": branch, "session_id": session_id}


def reconcile(conn: sqlite3.Connection) -> None:
    """Mark running rows closed when their agent is gone (tab closed by hand, crash)."""
    live = {(x.get("pane_id"), x.get("name")) for x in live_agents()}
    for row in conn.execute("SELECT * FROM agents WHERE status = 'running'").fetchall():
        if (row["pane_id"], row["slug"]) not in live:
            conn.execute("UPDATE agents SET status='closed', closed_at=?, close_note=? WHERE slug=?",
                         (now(), "agent no longer running in Herdr (closed outside herdr-agent)", row["slug"]))
    conn.commit()


def summarize(row: sqlite3.Row, live: dict | None) -> dict:
    d = dict(row)
    d["instructions"] = d["instructions"][:200]
    d["live_status"] = live.get("agent_status") if live else None
    d["resumable"] = bool(row["worktree_path"] and not row["worktree_removed_at"] and Path(row["worktree_path"]).exists())
    return d


def cmd_list(a) -> dict:
    conn = db()
    reconcile(conn)
    q = "SELECT * FROM agents" + ("" if a.all else " WHERE status = 'running'")
    if a.repo:
        q += (" AND" if "WHERE" in q else " WHERE") + " repo_path = ?"
    rows = conn.execute(q + " ORDER BY created_at DESC", (repo_root(a.repo),) if a.repo else ()).fetchall()
    live = {(x.get("pane_id"), x.get("name")): x for x in live_agents()}
    return {"agents": [summarize(r, live.get((r["pane_id"], r["slug"]))) for r in rows]}


def cmd_show(a) -> dict:
    conn = db()
    reconcile(conn)
    row = get_row(conn, a.slug)
    d = summarize(row, live_agent(row))
    d["instructions"] = row["instructions"]
    d["transcript"] = str(transcript(row) or "")
    d["last_message"] = last_message(row)
    return d


def cmd_close(a) -> dict:
    conn = db()
    row = get_row(conn, a.slug)
    live = live_agent(row)
    if live and live.get("agent_status") in ("working", "blocked") and not a.force:
        raise Fail(f"{a.slug} is {live['agent_status']}; confirm with the user, then pass --force")
    result = {"closed": a.slug}
    # Close the tab even if the agent already exited to a shell (e.g. after /exit).
    if row["tab_id"] and run("herdr", "tab", "get", row["tab_id"], check=False).returncode == 0:
        herdr("tab", "close", row["tab_id"])
    conn.execute("UPDATE agents SET status='closed', closed_at=COALESCE(?, closed_at), close_note=? WHERE slug=?",
                 (now() if row["status"] == "running" else None, "closed via herdr-agent", a.slug))
    conn.commit()

    wt, branch, repo = row["worktree_path"], row["branch"], row["repo_path"]
    if row["worktree_removed_at"] or not wt or not Path(wt).exists():
        result["worktree"] = "already gone"
        return result
    if not branch:
        result["worktree"] = "kept: branch unknown"
        return result
    dirty = git(wt, "status", "--porcelain").stdout.strip()
    if dirty:
        result["worktree"] = "kept: uncommitted changes in worktree (still resumable)"
        return result
    evidence = merge_evidence(repo, branch)
    if not evidence:
        result["worktree"] = "kept: no merge detected (still resumable)"
        return result
    git(repo, "worktree", "unlock", wt, check=False)
    git(repo, "worktree", "remove", wt)
    git(repo, "branch", "-D", branch)
    conn.execute("UPDATE agents SET worktree_removed_at=? WHERE slug=?", (now(), a.slug))
    conn.commit()
    result["worktree"] = f"removed worktree and branch {branch}: {evidence}"
    return result


def cmd_resume(a) -> dict:
    conn = db()
    reconcile(conn)
    row = get_row(conn, a.slug)
    if row["status"] == "running":
        raise Fail(f"{a.slug} is still running in {row['tab_id']}")
    wt = row["worktree_path"]
    if row["worktree_removed_at"] or not wt or not Path(wt).exists():
        raise Fail(f"{a.slug}'s worktree was removed, so its session can't be resumed")
    if any(x.get("name") == a.slug for x in live_agents()):
        raise Fail(f"a live Herdr agent is already named {a.slug}")
    ws_id = a.workspace or resolve_workspace(row["repo_path"])["workspace_id"]
    ws_id, tab_id, pane_id = open_tab(row["repo_path"], wt, a.slug, ws_id)
    run("herdr", "agent", "start", a.slug, "--kind", "claude", "--pane", pane_id, "--timeout", START_TIMEOUT_MS,
        "--", "--resume", row["session_id"], "--model", row["model"] or DEFAULT_MODEL)
    conn.execute(
        "UPDATE agents SET status='running', workspace_id=?, tab_id=?, pane_id=?, closed_at=NULL, close_note=NULL,"
        " resume_count=resume_count+1, last_resumed_at=? WHERE slug=?",
        (ws_id, tab_id, pane_id, now(), a.slug),
    )
    conn.commit()
    if a.prompt_file:
        submit_prompt(a.slug, pane_id, Path(a.prompt_file).read_text())
    return {"resumed": a.slug, "workspace_id": ws_id, "tab_id": tab_id, "pane_id": pane_id}


def main() -> None:
    p = argparse.ArgumentParser(prog="herdr_agent.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("launch")
    s.add_argument("slug")
    s.add_argument("--prompt-file", required=True)
    g = s.add_mutually_exclusive_group()
    g.add_argument("--repo", help="any path inside the target repo")
    g.add_argument("--here", action="store_true", help="repo of the cwd; tab in the current Herdr workspace")
    s.add_argument("--workspace", help="explicit Herdr workspace id")
    s.add_argument("--model", default=DEFAULT_MODEL, help="Claude model for the agent (passed to claude --model; default opus)")
    s.set_defaults(fn=cmd_launch)

    s = sub.add_parser("list")
    s.add_argument("--all", action="store_true", help="include closed agents")
    s.add_argument("--repo")
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("show")
    s.add_argument("slug")
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("close")
    s.add_argument("slug")
    s.add_argument("--force", action="store_true", help="close even if working/blocked")
    s.set_defaults(fn=cmd_close)

    s = sub.add_parser("resume")
    s.add_argument("slug")
    s.add_argument("--workspace")
    s.add_argument("--prompt-file", help="optional follow-up instructions to send after resuming")
    s.set_defaults(fn=cmd_resume)

    a = p.parse_args()
    try:
        print(json.dumps(a.fn(a), indent=2, default=str))
    except Fail as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
