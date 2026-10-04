"""Persist agent runs (state + tool calls) for tracing, debugging and evaluation."""

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shopping_agent.agent.orchestrator import RunStore
from shopping_agent.agent.state import AgentState
from shopping_agent.db.models import AgentRunRow, ToolCallRow


class SqlRunStore(RunStore):
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def save(self, state: AgentState) -> None:
        payload = state.model_dump(mode="json")
        values = {
            "status": state.status.value,
            "intent": state.intent.value if state.intent else None,
            "request": state.request,
            "demo_data": state.demo_data,
            "created_at": state.created_at,
            "finished_at": state.finished_at,
            "summary": state.summary(),
            "state": payload,
            "error": state.error,
        }
        async with self._sessions() as session, session.begin():
            await session.execute(
                pg_insert(AgentRunRow)
                .values(id=state.run_id, **values)
                .on_conflict_do_update(index_elements=["id"], set_=values)
            )
            if state.tool_calls:
                await session.execute(
                    pg_insert(ToolCallRow)
                    .values(
                        [
                            {
                                "id": c.id,
                                "run_id": state.run_id,
                                "tool": c.tool,
                                "args": c.args,
                                "status": c.status,
                                "latency_ms": c.latency_ms,
                                "error": c.error,
                                "cached": c.cached,
                                "evidence_count": c.evidence_count,
                                "started_at": c.started_at,
                            }
                            for c in state.tool_calls
                        ]
                    )
                    .on_conflict_do_nothing()
                )

    async def load(self, run_id: str) -> AgentState | None:
        async with self._sessions() as session:
            row = await session.get(AgentRunRow, run_id)
        return AgentState.model_validate(row.state) if row else None
