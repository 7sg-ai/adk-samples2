"""Server-controlled ADK session creation and chat execution."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService, InMemorySessionService
from google.genai.types import Part, UserContent

from financial_advisor.agent import root_agent


class AdkGateway:
    def __init__(self, session_service: BaseSessionService | None = None) -> None:
        self._session_service = session_service or InMemorySessionService()
        self._runner = Runner(
            agent=root_agent,
            app_name="financial_advisor",
            session_service=self._session_service,
        )

    async def create_session(
        self, *, user_id: str, state: dict[str, Any]
    ) -> str:
        session = await self._session_service.create_session(
            app_name=self._runner.app_name, user_id=user_id, state=state
        )
        return session.id

    async def get_state(self, *, user_id: str, session_id: str) -> dict[str, Any]:
        session = await self._session_service.get_session(
            app_name=self._runner.app_name,
            user_id=user_id,
            session_id=session_id,
        )
        if session is None:
            raise LookupError("ADK session not found")
        return dict(session.state)

    async def chat(
        self, *, user_id: str, session_id: str, message: str
    ) -> AsyncIterator[str]:
        content = UserContent(parts=[Part(text=message)])
        async for event in self._runner.run_async(
            user_id=user_id, session_id=session_id, new_message=content
        ):
            if not event.content or not event.content.parts:
                continue
            for part in event.content.parts:
                if part.text:
                    yield part.text