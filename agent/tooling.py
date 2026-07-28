"""Provider-neutral tool contract used by the agent runtime."""

from typing import Any, Protocol


class ToolInvocationError(Exception):
    """A tool provider failed in a way the model may recover from."""


class ToolRunner(Protocol):
    """Minimal catalog/invocation interface consumed by :class:`AgentLoop`."""

    @property
    def openai_tools(self) -> list[dict[str, Any]]: ...

    @property
    def tool_names(self) -> set[str]: ...

    async def call(self, name: str, arguments: dict[str, Any]) -> str: ...
