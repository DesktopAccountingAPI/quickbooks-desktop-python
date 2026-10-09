"""Cursor pagination.

The next page is requested only when the iteration needs it, so a loop that stops early never
sends an extra QuickBooks query. QuickBooks iterators expire when idle (``cursorExpiresAt``, about
10 seconds), so while you iterate items, a page held for more than ``_READ_AHEAD_AFTER`` seconds
makes the pager request the next page in the background. ``list_all()`` always requests the next
page as soon as a page arrives. A network error while fetching a page retries the same cursor,
which the server answers with the same page. ``410 CURSOR_EXPIRED`` raises
:class:`CursorExpiredError` with the progress so far; the pager never restarts a list on its own.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import time
from collections.abc import AsyncIterator, Iterator, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, Callable, Generic, Optional, TypeVar

import httpx

from ._errors import CursorExpiredError, DaapiError
from ._wire import parse_datetime, query_items

if TYPE_CHECKING:
    from ._base_client import AsyncAPIClient, Op, SyncAPIClient

T = TypeVar("T")

_READ_AHEAD_AFTER = 2.0
"""Seconds the item iterator holds a page before it requests the next one in the background."""


class CursorPage(Generic[T]):
    """One page of a cursor list."""

    def __init__(
        self,
        data: list[T],
        raw: Mapping[str, Any],
        raw_items: list[Any],
        response: httpx.Response,
        request_id: Optional[str] = None,
    ) -> None:
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
        self.request_id: Optional[str] = request_id or response.headers.get("daapi-request-id")
        """ID of the request that delivered this page: the response's ``Daapi-Request-Id``, or after a
        long poll the timed-out request's ID."""
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


def _build_page(
    data: Any,
    response: httpx.Response,
    parse_item: Callable[[Mapping[str, Any]], T],
    request_id: Optional[str] = None,
) -> CursorPage[T]:
    if not isinstance(data, Mapping):
        raise DaapiError("Expected a list object from the API.")
    raw_items = data.get("data")
    items = raw_items if isinstance(raw_items, list) else []
    return CursorPage([parse_item(i) for i in items], data, items, response, request_id)


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

    - ``for item in pager``: every item across all pages. The next page is requested when the loop
      reaches it, or in the background once a page has been held for 2 seconds.
    - ``pager.first_page()``: only the first page.
    - ``pager.iter_pages()``: page by page, each requested when you ask for it.
    - ``pager.list_all()``: every item, drained into a list (always reads one page ahead).
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
        data, response, request_id = self._client._fetch_page_data(self._op, self._path, params, self._options)
        return _build_page(data, response, self._parse_item, request_id)

    def first_page(self) -> CursorPage[T]:
        """Fetches the first page only."""
        return self._fetch(self._first_params)

    def _fetch_next(self, cursor: str, progress: _Progress, delivered: int) -> CursorPage[T]:
        try:
            return self._fetch(self._continue_params(cursor))
        except CursorExpiredError as error:
            _record_expiry(error, progress, delivered)
            raise

    def iter_pages(self) -> Iterator[CursorPage[T]]:
        """Yields page by page. The next page is requested when you ask for it."""
        progress = _Progress()
        delivered = 0
        page = self._fetch(self._first_params)
        while True:
            yield page
            delivered += 1
            progress.items += len(page.data)
            if page._raw_items:
                progress.last = page._raw_items[-1]
            if not (page.has_more and page.next_cursor):
                return
            page = self._fetch_next(page.next_cursor, progress, delivered)

    def __iter__(self) -> Iterator[T]:
        progress = _Progress()
        delivered = 0
        executor: Optional[ThreadPoolExecutor] = None
        pending: Optional[Future[CursorPage[T]]] = None
        try:
            page = self._fetch(self._first_params)
            while True:
                received = time.monotonic()
                cursor = page.next_cursor if page.has_more and page.next_cursor else None
                for index, item in enumerate(page.data):
                    # Read-ahead for slow consumers: the caller asked for another item and has held
                    # this page long enough that waiting for its end could let the cursor lapse.
                    if cursor and pending is None and time.monotonic() - received >= _READ_AHEAD_AFTER:
                        if executor is None:
                            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="daapi-read-ahead")
                        pending = executor.submit(self._fetch, self._continue_params(cursor))
                    progress.items += 1
                    progress.last = page._raw_items[index]
                    yield item
                delivered += 1
                if cursor is None:
                    return
                if pending is None:
                    page = self._fetch_next(cursor, progress, delivered)
                    continue
                try:
                    page = pending.result()
                except CursorExpiredError as error:
                    _record_expiry(error, progress, delivered)
                    raise
                finally:
                    pending = None
        finally:
            if pending is not None:
                pending.cancel()
            if executor is not None:
                executor.shutdown(wait=False)

    def list_all(self) -> list[T]:
        """Fetches every page and returns all items in one list, requesting each next page in the
        background as soon as a page arrives."""
        progress = _Progress()
        delivered = 0
        out: list[T] = []
        executor: Optional[ThreadPoolExecutor] = None
        pending: Optional[Future[CursorPage[T]]] = None
        try:
            page = self._fetch(self._first_params)
            while True:
                if page.has_more and page.next_cursor:
                    if executor is None:
                        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="daapi-read-ahead")
                    pending = executor.submit(self._fetch, self._continue_params(page.next_cursor))
                out.extend(page.data)
                delivered += 1
                progress.items += len(page.data)
                if page._raw_items:
                    progress.last = page._raw_items[-1]
                if pending is None:
                    return out
                try:
                    page = pending.result()
                except CursorExpiredError as error:
                    _record_expiry(error, progress, delivered)
                    raise
                finally:
                    pending = None
        finally:
            if pending is not None:
                pending.cancel()
            if executor is not None:
                executor.shutdown(wait=False)


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
        data, response, request_id = await self._client._fetch_page_data(self._op, self._path, params, self._options)
        return _build_page(data, response, self._parse_item, request_id)

    async def first_page(self) -> CursorPage[T]:
        """Fetches the first page only."""
        return await self._fetch(self._first_params)

    async def _fetch_next(self, cursor: str, progress: _Progress, delivered: int) -> CursorPage[T]:
        try:
            return await self._fetch(self._continue_params(cursor))
        except CursorExpiredError as error:
            _record_expiry(error, progress, delivered)
            raise

    async def iter_pages(self) -> AsyncIterator[CursorPage[T]]:
        """Yields page by page. The next page is requested when you ask for it."""
        progress = _Progress()
        delivered = 0
        page = await self._fetch(self._first_params)
        while True:
            yield page
            delivered += 1
            progress.items += len(page.data)
            if page._raw_items:
                progress.last = page._raw_items[-1]
            if not (page.has_more and page.next_cursor):
                return
            page = await self._fetch_next(page.next_cursor, progress, delivered)

    async def __aiter__(self) -> AsyncIterator[T]:
        progress = _Progress()
        delivered = 0
        pending: Optional[asyncio.Task[CursorPage[T]]] = None
        try:
            page = await self._fetch(self._first_params)
            while True:
                received = time.monotonic()
                cursor = page.next_cursor if page.has_more and page.next_cursor else None
                for index, item in enumerate(page.data):
                    # Read-ahead for slow consumers (see CursorPager.__iter__).
                    if cursor and pending is None and time.monotonic() - received >= _READ_AHEAD_AFTER:
                        pending = asyncio.ensure_future(self._fetch(self._continue_params(cursor)))
                    progress.items += 1
                    progress.last = page._raw_items[index]
                    yield item
                delivered += 1
                if cursor is None:
                    return
                if pending is None:
                    page = await self._fetch_next(cursor, progress, delivered)
                    continue
                try:
                    page = await pending
                except CursorExpiredError as error:
                    _record_expiry(error, progress, delivered)
                    raise
                finally:
                    pending = None
        finally:
            if pending is not None and not pending.done():
                pending.cancel()

    async def list_all(self) -> list[T]:
        """Fetches every page and returns all items in one list, requesting each next page in a
        background task as soon as a page arrives."""
        progress = _Progress()
        delivered = 0
        out: list[T] = []
        pending: Optional[asyncio.Task[CursorPage[T]]] = None
        try:
            page = await self._fetch(self._first_params)
            while True:
                if page.has_more and page.next_cursor:
                    pending = asyncio.ensure_future(self._fetch(self._continue_params(page.next_cursor)))
                out.extend(page.data)
                delivered += 1
                progress.items += len(page.data)
                if page._raw_items:
                    progress.last = page._raw_items[-1]
                if pending is None:
                    return out
                try:
                    page = await pending
                except CursorExpiredError as error:
                    _record_expiry(error, progress, delivered)
                    raise
                finally:
                    pending = None
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
