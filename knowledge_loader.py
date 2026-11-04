"""Load Markdown knowledge into a stable prompt prefix.

Use a deterministic file order without timestamps or random content. A stable
prefix enables provider-side caching but does not guarantee a cache hit."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

KNOWLEDGE_DIR = Path(__file__).resolve().parent / "knowledge"

# Keep file ordering deterministic for a stable prefix.
KNOWLEDGE_FILES = (
    "products.md",
    "pricing.md",
    "shipping_returns.md",
    "faq.md",
)


class KnowledgeBase:
    """An immutable knowledge snapshot."""

    def __init__(self, docs: dict[str, str]) -> None:
        self._docs = dict(docs)

    @property
    def files(self) -> List[str]:
        return [name for name, content in self._docs.items() if content.strip()]

    def is_empty(self) -> bool:
        return not any(text.strip() for text in self._docs.values())

    def render(self) -> str:
        """Render a stable Markdown block for the system prompt."""
        parts: List[str] = []
        for name in self._docs:
            text = self._docs[name].strip()
            if not text:
                continue
            parts.append(f"### File: knowledge/{name}\n\n{text}")
        return "\n\n---\n\n".join(parts)


def load_knowledge(knowledge_dir: Optional[Path] = None) -> KnowledgeBase:
    """Load the predefined knowledge files; warn about missing files."""
    base = Path(knowledge_dir) if knowledge_dir else KNOWLEDGE_DIR
    docs: dict[str, str] = {}
    for name in KNOWLEDGE_FILES:
        path = base / name
        try:
            docs[name] = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.warning("Knowledge file missing: %s", path)
            docs[name] = ""
    return KnowledgeBase(docs)
