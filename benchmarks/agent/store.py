"""Task preparation, answer storage, and replay for agent-answered benchmark runs."""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from benchmarks.generate import Variant, generate_suite
from schemagate.errors import ProviderResponseError
from schemagate.ingest.profile import profile_table
from schemagate.llm import MappingRequest, ProviderResponse
from schemagate.resolve import ModelResolver, ResolverChain
from schemagate.schema.target import TargetSchema

DATA_DIR = Path(__file__).resolve().parent.parent / "agent_runs"
RUN_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
Suite = list[tuple[TargetSchema, Variant]]


def request_key(system_prompt: str, user_prompt: str) -> str:
    return hashlib.sha256(json.dumps([system_prompt, user_prompt]).encode("utf-8")).hexdigest()


def build_suites(config: dict[str, Any]) -> dict[str, Suite]:
    """Rebuild the benchmark suites deterministically from the recorded configuration."""
    suites = {"synthetic": generate_suite(config["variants"], config["seed"])}
    if config.get("valentine"):
        from benchmarks.valentine import load_valentine_groups

        groups = load_valentine_groups(Path(config["valentine"]), per_group=config["per_group"])
        suites["valentine"] = [pair for g in sorted(groups) for pair in groups[g]]
    return suites


class _Recorder:
    """Stands in for a provider during ``prepare``: records each request, answers nothing."""

    model_id = "recorder"

    def __init__(self) -> None:
        self.requests: list[MappingRequest] = []

    def complete(self, request: MappingRequest) -> ProviderResponse:
        self.requests.append(request)
        raise ProviderResponseError("recording only")


def prepare(config: dict[str, Any], data_dir: Path = DATA_DIR) -> dict[str, Any]:
    tasks = []
    for suite_name, suite in build_suites(config).items():
        for i, (schema, variant) in enumerate(suite):
            recorder = _Recorder()
            ResolverChain.default(ModelResolver(recorder)).run(
                schema, profile_table(variant.columns, variant.rows))
            for request in recorder.requests:
                tasks.append({
                    "task_id": f"{suite_name[:3]}-{i:03d}",
                    "suite": suite_name,
                    "key": request_key(request.system_prompt, request.user_prompt),
                    "system_prompt": request.system_prompt,
                    "user_prompt": request.user_prompt,
                    "fields": [f.name for f in request.fields],
                    "columns": [c.name for c in request.columns],
                    "already_mapped": request.already_mapped,
                    "prompt_version": request.prompt.version,
                })
    doc = {"config": config, "tasks": tasks}
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "tasks.json").write_text(json.dumps(doc, indent=1) + "\n", "utf-8")
    return doc


def load_tasks(data_dir: Path = DATA_DIR) -> dict[str, Any]:
    path = data_dir / "tasks.json"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found; run: python -m benchmarks.agent prepare")
    return json.loads(path.read_text("utf-8"))


def check_run(run: str) -> str:
    if not RUN_NAME.match(run):
        raise ValueError("run must be 1-40 characters of letters, digits, '.', '_' or '-'")
    return run


def answer_path(run: str, task_id: str, data_dir: Path = DATA_DIR) -> Path:
    return data_dir / "answers" / check_run(run) / f"{task_id}.json"


def save_answer(run: str, task: dict[str, Any], answer: str, model: str | None,
                data_dir: Path = DATA_DIR) -> None:
    path = answer_path(run, task["task_id"], data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"task_id": task["task_id"], "key": task["key"], "answer": answer,
                                "model": model, "answered_at": time.time()}) + "\n", "utf-8")


def load_answers(run: str, data_dir: Path = DATA_DIR) -> dict[str, dict[str, Any]]:
    """Answers for a run, keyed by request key."""
    folder = data_dir / "answers" / check_run(run)
    out = {}
    for path in sorted(folder.glob("*.json")) if folder.exists() else []:
        doc = json.loads(path.read_text("utf-8"))
        out[doc["key"]] = doc
    return out


class ReplayProvider:
    """Answers the model tier from a run's stored agent answers."""

    def __init__(self, run: str, data_dir: Path = DATA_DIR) -> None:
        self.run = run
        self.answers = load_answers(run, data_dir)
        self.model_id = f"agent:{run}"
        self.served = 0
        self.missing = 0

    def complete(self, request: MappingRequest) -> ProviderResponse:
        doc = self.answers.get(request_key(request.system_prompt, request.user_prompt))
        if doc is None:
            self.missing += 1
            raise ProviderResponseError("no stored answer for this request")
        self.served += 1
        return ProviderResponse(doc["answer"], f"agent:{self.run}:{doc.get('model') or 'unknown'}")


def task_request(task: dict[str, Any]) -> Any:
    """The parts of a MappingRequest that response validation looks at."""
    from types import SimpleNamespace

    return SimpleNamespace(
        fields=[SimpleNamespace(name=n) for n in task["fields"]],
        columns=[SimpleNamespace(name=n) for n in task["columns"]],
        already_mapped=dict(task["already_mapped"]),
    )


__all__ = ["DATA_DIR", "ReplayProvider", "build_suites", "load_answers", "load_tasks",
           "prepare", "save_answer", "task_request"]
