"""Cursor pagination with one page of read-ahead.

QuickBooks iterators expire when idle (``cursorExpiresAt``), so the pager requests page N+1 as
soon as page N arrives. A network error while fetching a page retries the same cursor, which the
server answers with the same page. ``410 CURSOR_EXPIRED`` raises :class:`CursorExpiredError` with
the progress so far; the pager never restarts a list on its own.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
from collections.abc import AsyncIterator, Iterator, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, Callable, Generic, Optional, TypeVar

import httpx

from ._errors import CursorExpiredError, DaapiError
from ._wire import parse_datetime, query_items

if TYPE_CHECKING:
    from ._base_client import AsyncAPIClient, Op, SyncAPIClient

T = TypeVar("T")


class CursorPage(Generic[T]):
    """One page of a cursor list."""

    def __init__(self, data: list[T], raw: Mapping[str, Any], raw_items: list[Any], response: httpx.Response) -> None:
        self.data = data
        """The items on this page."""
        next_cursor = raw.get("nextCursor")
        self.next_cursor: Optional[str] = next_cursor if isinstance(next_cursor, str) else None
        """Cursor for the next page, ``None`` on the last page."""
        self.has_more: bool = raw.get("hasMore") is True
        """Whether more pages follow."""
        remaining = raw.get("remainingCount")
        self.remaining_count: Optional[int] = remaining if isinstance(remaining, int) else None
        """Records left after this page (QuickBooks lists), ``None`` on the last page or when unknown."""
        expires = parse_datetime(raw.get("cursorExpiresAt"))
        self.cursor_expires_at: Optional[_dt.datetime] = expires if isinstance(expires, _dt.datetime) else None
        """The server's estimate of when the cursor expires if no continue request arrives."""
        self.request_id: Optional[str] = response.headers.get("daapi-request-id")
        """``Daapi-Request-Id`` of the response that delivered this page."""
        self._raw_items = raw_items

    def __iter__(self) -> Iterator[T]:
        return iter(self.data)

    def __len__(self) -> int:
        return len(self.data)

    def __repr__(self) -> str:
        return f"<CursorPage items={len(self.data)} has_more={self.has_more} next_cursor={self.next_cursor!r}>"


class _Progress:
    __slots__ = ("items", "last")

    def __init__(self) -> None:
        self.items = 0
        self.last: Any = None


def _build_page(data: Any, response: httpx.Response, parse_item: Callable[[Mapping[str, Any]], T]) -> CursorPage[T]:
    if not isinstance(data, Mapping):
        raise DaapiError("Expected a list object from the API.")
    raw_items = data.get("data")
    items = raw_items if isinstance(raw_items, list) else []
    return CursorPage([parse_item(i) for i in items], data, items, response)


def _record_expiry(error: CursorExpiredError, progress: _Progress, pages: int) -> None:
    last = progress.last if isinstance(progress.last, Mapping) else {}
    last_id = last.get("id")
    updated = last.get("updatedAt")
    error._set_progress(
        items_yielded=progress.items,
        pages_delivered=pages,
        last_id=last_id if isinstance(last_id, str) else None,
        last_updated_at=updated if isinstance(updated, str) else None,
    )


class _PagerBase(Generic[T]):
    def __init__(
        self,
        op: Op,
        path: str,
        query: Mapping[str, Any],
        parse_item: Callable[[Mapping[str, Any]], T],
        options: Mapping[str, Any],
    ) -> None:
        self._op = op
        self._path = path
        self._first_params = query_items(query)
        limit = query.get("limit")
        self._limit = limit if isinstance(limit, int) and not isinstance(limit, bool) else None
        self._parse_item = parse_item
        self._options = dict(options)

    def _continue_params(self, cursor: str) -> list[tuple[str, str]]:
        # Continue requests send only the cursor (and the caller's limit); filters are fixed by the cursor.
        params = [("cursor", cursor)]
        if self._limit is not None:
            params.append(("limit", str(self._limit)))
        return params


class CursorPager(_PagerBase[T]):
    """Iterates a cursor list. Nothing is fetched until you iterate or call ``first_page()``.

    - ``for item in pager``: every item across all pages (one page of read-ahead).
    - ``pager.first_page()``: only the first page.
    - ``pager.iter_pages()``: page by page.
    - ``pager.list_all()``: every item, drained into a list.
    """

    def __init__(
        self,
        client: SyncAPIClient,
        op: Op,
        path: str,
        query: Mapping[str, Any],
        parse_item: Callable[[Mapping[str, Any]], T],
        options: Mapping[str, Any],
    ) -> None:
        super().__init__(op, path, query, parse_item, options)
        self._client = client

    def _fetch(self, params: list[tuple[str, str]]) -> CursorPage[T]:
        data, response = self._client._fetch_page_data(self._op, self._path, params, self._options)
        return _build_page(data, response, self._parse_item)

    def first_page(self) -> CursorPage[T]:
        """Fetches the first page only."""
        return self._fetch(self._first_params)

    def iter_pages(self) -> Iterator[CursorPage[T]]:
        """Yields page by page, fetching the next page in the background as soon as a page arrives."""
        return self._pages(None)

    def _pages(self, progress: Optional[_Progress]) -> Iterator[CursorPage[T]]:
        per_page = progress is None
        tracked = progress if progress is not None else _Progress()
        executor: Optional[ThreadPoolExecutor] = None
        pending: Optional[Future[CursorPage[T]]] = None
        delivered = 0
        try:
            page = self._fetch(self._first_params)
            while True:
                pending = None
                if page.has_more and page.next_cursor:
                    if executor is None:
                        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="daapi-read-ahead")
                    pending = executor.submit(self._fetch, self._continue_params(page.next_cursor))
                yield page
                delivered += 1
                if per_page:
                    tracked.items += len(page.data)
                    if page._raw_items:
                        tracked.last = page._raw_items[-1]
                if pending is None:
                    return
                try:
                    page = pending.result()
                except CursorExpiredError as error:
                    _record_expiry(error, tracked, delivered)
                    raise
        finally:
            if pending is not None:
                pending.cancel()
            if executor is not None:
                executor.shutdown(wait=False)

    def __iter__(self) -> Iterator[T]:
        progress = _Progress()
        for page in self._pages(progress):
            for index, item in enumerate(page.data):
                progress.items += 1
                progress.last = page._raw_items[index]
                yield item

    def list_all(self) -> list[T]:
        """Fetches every page and returns all items in one list."""
        return list(self)


class AsyncCursorPager(_PagerBase[T]):
    """Async counterpart of :class:`CursorPager`: ``async for item in pager``, ``await pager.first_page()``,
    ``async for page in pager.iter_pages()``, ``await pager.list_all()``."""

    def __init__(
        self,
        client: AsyncAPIClient,
        op: Op,
        path: str,
        query: Mapping[str, Any],
        parse_item: Callable[[Mapping[str, Any]], T],
        options: Mapping[str, Any],
    ) -> None:
        super().__init__(op, path, query, parse_item, options)
        self._client = client

    async def _fetch(self, params: list[tuple[str, str]]) -> CursorPage[T]:
        data, response = await self._client._fetch_page_data(self._op, self._path, params, self._options)
        return _build_page(data, response, self._parse_item)

    async def first_page(self) -> CursorPage[T]:
        """Fetches the first page only."""
        return await self._fetch(self._first_params)

    def iter_pages(self) -> AsyncIterator[CursorPage[T]]:
        """Yields page by page, fetching the next page in a background task as soon as a page arrives."""
        return self._pages(None)

    async def _pages(self, progress: Optional[_Progress]) -> AsyncIterator[CursorPage[T]]:
        per_page = progress is None
        tracked = progress if progress is not None else _Progress()
        pending: Optional[asyncio.Task[CursorPage[T]]] = None
        delivered = 0
        try:
            page = await self._fetch(self._first_params)
            while True:
                pending = None
                if page.has_more and page.next_cursor:
                    pending = asyncio.ensure_future(self._fetch(self._continue_params(page.next_cursor)))
                yield page
                delivered += 1
                if per_page:
                    tracked.items += len(page.data)
                    if page._raw_items:
                        tracked.last = page._raw_items[-1]
                if pending is None:
                    return
                try:
                    page = await pending
                except CursorExpiredError as error:
                    _record_expiry(error, tracked, delivered)
                    raise
                finally:
                    pending = None
        finally:
            if pending is not None and not pending.done():
                pending.cancel()

    async def __aiter__(self) -> AsyncIterator[T]:
        progress = _Progress()
        async for page in self._pages(progress):
            for index, item in enumerate(page.data):
                progress.items += 1
                progress.last = page._raw_items[index]
                yield item

    async def list_all(self) -> list[T]:
        """Fetches every page and returns all items in one list."""
        return [item async for item in self]
