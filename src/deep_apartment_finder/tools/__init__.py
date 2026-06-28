"""Tool factories.

Each agent-callable action is a thin function around an injected
dependency. Tools are constructed by `make_*_tool(dep)` factories so
the agent code never imports concrete adapters; it just receives a
list of `BaseTool` objects.

We use closure-based dependency injection (the factory captures the
`ApartmentRepository` / `ScraperPort`). The alternative — `ToolRuntime`
— is the LangChain mechanism for *system* dependencies (store, config)
not application ones.
"""

from __future__ import annotations
