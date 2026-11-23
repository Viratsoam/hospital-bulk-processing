"""Generic in-memory repository.

Defines the storage seam for the application. Today the only implementation
keeps records in a dictionary, which satisfies the task's in-memory
requirement; a durable backend can later satisfy the same interface without
touching callers.
"""

from abc import ABC, abstractmethod
from typing import Dict, Generic, List, Optional, TypeVar

Key = TypeVar("Key")
Value = TypeVar("Value")


class Repository(ABC, Generic[Key, Value]):
    """Minimal key-value persistence contract."""

    @abstractmethod
    def add(self, key: Key, value: Value) -> Value:
        """Store a value under ``key``, replacing any existing entry."""

    @abstractmethod
    def get(self, key: Key) -> Optional[Value]:
        """Return the value for ``key``, or ``None`` when absent."""

    @abstractmethod
    def list(self) -> List[Value]:
        """Return every stored value."""

    @abstractmethod
    def delete(self, key: Key) -> bool:
        """Remove ``key``; return True when an entry was removed."""

    @abstractmethod
    def clear(self) -> None:
        """Remove every entry."""


class InMemoryRepository(Repository[Key, Value]):
    """Dictionary-backed repository.

    Not safe for concurrent multi-process use, and contents are lost on
    restart. Both limitations are acceptable for the current scope and are
    documented rather than hidden.
    """

    def __init__(self) -> None:
        """Initialize an empty store."""
        self._items: Dict[Key, Value] = {}

    def add(self, key: Key, value: Value) -> Value:
        """Store a value under ``key``.

        Args:
            key: Lookup key.
            value: Value to store.

        Returns:
            The stored value, to allow inline use.
        """
        self._items[key] = value
        return value

    def get(self, key: Key) -> Optional[Value]:
        """Return the value for ``key``.

        Args:
            key: Lookup key.

        Returns:
            The stored value, or ``None`` when absent.
        """
        return self._items.get(key)

    def list(self) -> List[Value]:
        """Return every stored value in insertion order.

        Returns:
            A new list, so callers cannot mutate internal state.
        """
        return list(self._items.values())

    def delete(self, key: Key) -> bool:
        """Remove ``key``.

        Args:
            key: Lookup key.

        Returns:
            True when an entry existed and was removed.
        """
        return self._items.pop(key, None) is not None

    def clear(self) -> None:
        """Remove every entry."""
        self._items.clear()

    def __len__(self) -> int:
        """Return the number of stored entries."""
        return len(self._items)
