from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

DEFAULT_PROMPT = "map_columns"
DEFAULT_PROMPT_REVISION = 1


@dataclass(frozen=True)
class PromptTemplate:
    name: str
    revision: int
    system: str
    user: str

    @property
    def version(self) -> str:
        digest = hashlib.sha256((self.system + self.user).encode("utf-8")).hexdigest()[:8]
        return f"{self.name}/v{self.revision}+{digest}"

    def render_user(self, **kwargs: str) -> str:
        return self.user.format(**kwargs)


@lru_cache(maxsize=16)
def load_prompt(name: str = DEFAULT_PROMPT, revision: int = DEFAULT_PROMPT_REVISION) -> PromptTemplate:
    text = (
        resources.files("schemagate.llm").joinpath(f"prompts/{name}_v{revision}.txt").read_text("utf-8")
    )
    _, rest = text.split("=== system ===\n", 1)
    system, user = rest.split("=== user ===\n", 1)
    return PromptTemplate(name=name, revision=revision, system=system.strip(), user=user.strip())
