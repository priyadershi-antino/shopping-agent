"""Versioned prompts, one file per responsibility. Templates use $placeholders."""

import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from string import Template

_VERSION = re.compile(r"^<!--\s*version:\s*(\d+)\s*-->\s*\n", re.IGNORECASE)


@dataclass(frozen=True)
class Prompt:
    name: str
    version: int
    template: str

    def render(self, **values: str) -> str:
        return Template(self.template).substitute(values)

    @property
    def placeholders(self) -> set[str]:
        return {
            m.group("named") or m.group("braced")
            for m in Template.pattern.finditer(self.template)
            if m.group("named") or m.group("braced")
        }


@lru_cache
def load_prompt(name: str) -> Prompt:
    """Load `prompts/<name>.md`, e.g. load_prompt("agent/system")."""
    *folders, stem = name.split("/")
    text = resources.files(__package__).joinpath(*folders, f"{stem}.md").read_text(encoding="utf-8")
    match = _VERSION.match(text)
    if not match:
        raise ValueError(f"prompt {name} has no version header")
    return Prompt(name=name, version=int(match.group(1)), template=text[match.end() :].strip())


ALL_PROMPTS = (
    "agent/system",
    "agent/planning",
    "agent/research",
    "agent/synthesis",
    "requirements/extraction",
    "reviews/analysis",
    "deals/offer_extraction",
)
