from typing import Sequence

from langchain_core.chat_history import BaseChatMessageHistory
from langchain_core.messages import BaseMessage
from langchain_core.pydantic_v1 import BaseModel, Field

_store = {}

__all__ = ("InMemoryHistory", "get_by_session_id")


class InMemoryHistory(BaseChatMessageHistory, BaseModel):
    """In memory implementation of chat message history."""

    messages: list[BaseMessage] = Field(default_factory=list)

    def __init__(self, memory: int) -> None:
        self.memory = memory

    def add_messages(self, messages: Sequence[BaseMessage]) -> None:
        """Add a list of messages to the store

        Args:
            messages (Sequence[BaseMessage]): A list of BaseMessage objects to store.

        """
        for message in messages:
            self.messages.append(message)
        if self.memory > 0:
            self.messages = self.messages[-self.memory :]

    def clear(self) -> None:
        self.messages = []


def get_by_session_id(session_id: str) -> BaseChatMessageHistory:
    if session_id not in _store:
        _store[session_id] = InMemoryHistory()
    return _store[session_id]
