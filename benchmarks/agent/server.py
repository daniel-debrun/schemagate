"""MCP server that lets a coding agent act as schemagate's model tier.

Register with Claude Code (stdio):

    claude mcp add -s user schemagate-bench -- /path/to/.venv/bin/python /path/to/benchmarks/agent/server.py

Each agent works under its own run name (for example ``haiku`` or ``sonnet``). ``next_task`` returns the
exact system and user prompt schemagate would send to a model; ``submit_answer`` stores the reply after
the same structural checks the real providers get. Feedback is structural only (invalid JSON, wrong
shape); the server never says whether a mapping is correct.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):  # run as a script by the MCP client
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mcp.server.mcpserver import MCPServer

from benchmarks.agent.store import check_run, load_answers, load_tasks, save_answer, task_request
from schemagate.errors import ProviderResponseError
from schemagate.llm.validation import validate_response

INSTRUCTIONS = """\
You are answering schema-mapping questions for the schemagate benchmark. Loop: call next_task with your
run name, read its system_prompt and user_prompt, reason only from what they contain, and call
submit_answer with your JSON reply (the format is defined in system_prompt). Repeat until next_task
reports done. Do not read files, search the repository, or look for ground truth: answer from the
prompt alone, as a model behind an API would."""

server = MCPServer("schemagate-bench", instructions=INSTRUCTIONS)


def _tasks() -> list[dict]:
    return load_tasks()["tasks"]


def _progress(run: str) -> dict:
    tasks = _tasks()
    done = {doc["task_id"] for doc in load_answers(check_run(run)).values()}
    return {"run": run, "answered": sum(t["task_id"] in done for t in tasks), "total": len(tasks)}


@server.tool()
def bench_overview() -> dict:
    """How many benchmark questions exist, and how far each run has got."""
    from benchmarks.agent.store import DATA_DIR

    runs = sorted(p.name for p in (DATA_DIR / "answers").glob("*")) if (DATA_DIR / "answers").exists() else []
    tasks = _tasks()
    return {"total_tasks": len(tasks), "suites": sorted({t["suite"] for t in tasks}),
            "runs": [_progress(r) for r in runs], "instructions": INSTRUCTIONS}


@server.tool()
def next_task(run: str, shard: int = 0, shards: int = 1) -> dict:
    """Return the next unanswered question for this run: task_id, system_prompt, user_prompt.

    Use a fixed run name for the whole session, e.g. "haiku" or "sonnet". When several agents share
    a run, give each a distinct shard in 0..shards-1 so they never get the same question. Returns
    done=true when every question in your shard has an answer.
    """
    if shards < 1 or not 0 <= shard < shards:
        return {"done": False, "error": "need shards >= 1 and 0 <= shard < shards"}
    answered = {doc["task_id"] for doc in load_answers(check_run(run)).values()}
    pending = [t for i, t in enumerate(_tasks()) if i % shards == shard and t["task_id"] not in answered]
    if not pending:
        return {"done": True, "shard": shard, "shards": shards, **_progress(run)}
    t = pending[0]
    return {"done": False, "task_id": t["task_id"], "remaining": len(pending),
            "system_prompt": t["system_prompt"], "user_prompt": t["user_prompt"]}


@server.tool()
def submit_answer(run: str, task_id: str, answer: str, model: str = "") -> dict:
    """Store your reply to a question. `answer` is the raw JSON text the system prompt asks for.

    `model` is the model you are running as (for the record). Structurally invalid replies are not
    stored and the error is returned so you can resend; semantic correctness is never reported.
    """
    task = next((t for t in _tasks() if t["task_id"] == task_id), None)
    if task is None:
        return {"saved": False, "error": f"unknown task_id {task_id!r}"}
    try:
        validated = validate_response(answer, task_request(task), cap=None)
    except ProviderResponseError as exc:
        return {"saved": False, "error": str(exc)}
    save_answer(check_run(run), task, answer, model or None)
    answered = {v.source_column for v in validated}
    missing = [c for c in task["columns"] if c not in answered]
    return {"saved": True, "columns_answered": len(answered), "columns_total": len(task["columns"]),
            "columns_missing": missing, **_progress(run)}


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
