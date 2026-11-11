"""Atomic database views for composing existing domain services."""
from contextlib import contextmanager

from .store import Store


class TransactionStore(Store):
    """Reuse one outer transaction; inner service scopes never commit independently."""

    def __init__(self, parent: Store, connection):
        self.path, self.lock, self._db = parent.path, parent.lock, connection

    @contextmanager
    def connection(self):
        yield self._db
