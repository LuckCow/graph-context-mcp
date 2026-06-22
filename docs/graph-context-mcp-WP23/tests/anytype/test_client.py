"""Client behavior: pagination, retry, error translation, budgets."""

import pytest

from graph_context.infrastructure.anytype.config import AnytypeApiError


class TestPagination:
    async def test_paginate_stitches_pages(self, mock, client, repo):
        # page_limit=10; seed 25 humans -> 25 + bootstrap-visible objects
        for i in range(25):
            mock.seed_object("gc_character", f"extra-{i}")
        items = [o async for o in client.list_objects()]
        assert len(items) == 25
        # multiple GET pages were issued
        list_calls = [p for m, p in mock.request_log if m == "GET" and p.endswith("/objects")]
        assert len(list_calls) >= 3


class TestRetry:
    async def test_retries_on_429_then_succeeds(self, mock, client):
        mock.fail_next(2, status=429)
        items = [o async for o in client.list_objects()]
        assert items == []  # call ultimately succeeded

    async def test_exhausted_retries_raise_api_error(self, mock, client):
        mock.fail_next(10, status=429)
        with pytest.raises(AnytypeApiError) as excinfo:
            [o async for o in client.list_objects()]
        assert excinfo.value.status == 429
        assert excinfo.value.code == "rate_limit_exceeded"

    async def test_non_retryable_errors_raise_immediately(self, mock, client):
        mock.fail_next(1, status=404)
        before = len(mock.request_log)
        with pytest.raises(AnytypeApiError):
            await client.get_object("anything")
        assert len(mock.request_log) == before + 1  # exactly one attempt
