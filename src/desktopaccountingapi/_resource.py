"""Base classes of the generated resources."""

from __future__ import annotations

from ._base_client import AsyncAPIClient, SyncAPIClient


class Resource:
    """A group of operations on the synchronous client, for example ``client.qbd.invoices``."""

    def __init__(self, client: SyncAPIClient) -> None:
        self._client = client


class AsyncResource:
    """A group of operations on the asynchronous client, for example ``client.qbd.invoices``."""

    def __init__(self, client: AsyncAPIClient) -> None:
        self._client = client
