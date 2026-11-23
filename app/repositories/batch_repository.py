"""Persistence seam for batch records.

Defines the storage contract for bulk-processing batches. The reference
implementation is in-memory; a durable backend (database, object store, ...)
can later satisfy the same interface without touching the processing service.

Retrieval never hands back the stored instance: it returns a deep copy, so
callers mutating a record cannot corrupt tracked state.
"""

from abc import ABC, abstractmethod
from typing import List, Optional
from uuid import UUID

from app.schemas.batch import BatchRecord


class BatchRepository(ABC):
    """Key-value persistence contract for batch records, keyed by batch UUID."""

    @abstractmethod
    def save(self, batch: BatchRecord) -> None:
        """Store or replace the record for ``batch.batch_id``.

        Args:
            batch: The full record to persist.
        """

    @abstractmethod
    def get(self, batch_id: UUID) -> Optional[BatchRecord]:
        """Return a defensive copy of the batch, or ``None`` when absent.

        Args:
            batch_id: The batch UUID.

        Returns:
            A fresh :class:`BatchRecord`, safe to mutate by the caller.
        """

    @abstractmethod
    def list(self) -> List[BatchRecord]:
        """Return defensive copies of every stored batch, in save order."""

    @abstractmethod
    def clear(self) -> None:
        """Remove every stored batch."""


class InMemoryBatchRepository(BatchRepository):
    """Dictionary-backed batch repository.

    Not safe for multi-process use and contents are lost on restart; both are
    acceptable for the current scope. Retrieval methods return deep copies so
    the internal store can never be mutated through a returned record.
    """

    def __init__(self) -> None:
        """Initialize an empty store."""
        from collections import OrderedDict

        self._items: "OrderedDict[UUID, BatchRecord]" = OrderedDict()

    def save(self, batch: BatchRecord) -> None:
        """Store or replace the record under ``batch.batch_id``."""
        self._items[batch.batch_id] = batch

    def get(self, batch_id: UUID) -> Optional[BatchRecord]:
        """Return a deep copy of the batch, or ``None`` when absent."""
        record = self._items.get(batch_id)
        return record.model_copy(deep=True) if record is not None else None

    def list(self) -> List[BatchRecord]:
        """Return deep copies of every stored batch, in save order."""
        return [record.model_copy(deep=True) for record in self._items.values()]

    def clear(self) -> None:
        """Remove every stored batch."""
        self._items.clear()

    def __len__(self) -> int:
        """Return the number of stored batches."""
        return len(self._items)
