from typing import Protocol, runtime_checkable


@runtime_checkable
class ContextSource(Protocol):
    """Always called on every /chat request; injects retrieved text into Claude's prompt."""
    name: str

    def fetch(self, question: str, embedding: list) -> str: ...


@runtime_checkable
class ToolSource(Protocol):
    """Exposed as a Claude tool; Claude decides when to invoke it."""
    name: str

    def tool_definition(self) -> dict: ...

    def execute(self, tool_input: dict) -> str: ...


# Populated by app/resources.py at startup
CONTEXT_SOURCES: list = []   # ContextSource instances  e.g. [RAGSource()]
TOOL_SOURCES: list = []      # ToolSource instances      e.g. [SocrataSource()]
