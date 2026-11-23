import asyncio

import pytest

from ai_ingest.net import AsyncFetcher, CircuitBreaker, SourcePolicy


class FakeResponse:
    def __init__(self, status, body=b"", headers=None, url=""):
        self.status = status
        self._body = body
        self.headers = headers or {}
        self.url = url
        self.content = self

    async def read(self, _limit=-1):
        return self._body

    def iter_chunked(self, _size):
        async def generate():
            yield self._body

        return generate()

    async def text(self):
        return self._body.decode("utf-8", "replace")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        queue = self.routes.get(url)
        if queue is None:
            return FakeResponse(404, b"missing")
        if isinstance(queue, list):
            return queue.pop(0) if len(queue) > 1 else queue[0]
        return queue

    async def close(self):
        return None


def policy(**overrides):
    base = dict(
        name="example",
        domain="example.com",
        max_concurrency=2,
        min_interval=0.0,
        max_attempts=3,
        retry_budget=2,
    )
    base.update(overrides)
    return SourcePolicy(**base)


def test_robots_disallow_blocks_page_fetch():
    session = FakeSession(
        {
            "https://example.com/robots.txt": FakeResponse(
                200, b"User-agent: *\nDisallow: /private\n"
            ),
            "https://example.com/private/page": FakeResponse(200, b"secret"),
            "https://example.com/public": FakeResponse(200, b"public"),
        }
    )

    async def run():
        async with AsyncFetcher(
            policies={"example": policy()},
            user_agent="ai-intelligence-ingestion/0.2",
            session=session,
        ) as fetcher:
            blocked = await fetcher.fetch("example", "https://example.com/private/page")
            allowed = await fetcher.fetch("example", "https://example.com/public")
            return blocked, allowed, fetcher.report()

    blocked, allowed, report = asyncio.run(run())

    assert blocked.blocked_reason == "ROBOTS_DISALLOWED"
    assert blocked.status is None
    assert allowed.ok
    assert report["metrics"]["example"]["blocked_by_robots"] == 1


def test_429_retries_with_backoff_then_succeeds(monkeypatch):
    import ai_ingest.net as net

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(net.asyncio, "sleep", no_sleep)

    session = FakeSession(
        {
            "https://example.com/robots.txt": FakeResponse(404),
            "https://example.com/data": [
                FakeResponse(429, b"slow down", {"Retry-After": "0"}),
                FakeResponse(200, b"ok"),
            ],
        }
    )

    async def run():
        async with AsyncFetcher(
            policies={"example": policy()},
            user_agent="ai-intelligence-ingestion/0.2",
            session=session,
        ) as fetcher:
            result = await fetcher.fetch("example", "https://example.com/data")
            return result, fetcher.report()

    result, report = asyncio.run(run())

    assert result.ok
    assert result.retries == 1
    assert report["metrics"]["example"]["rate_limit_events"] == 1


def test_retry_budget_stops_request_storm(monkeypatch):
    import ai_ingest.net as net

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(net.asyncio, "sleep", no_sleep)

    session = FakeSession(
        {
            "https://example.com/robots.txt": FakeResponse(404),
            "https://example.com/data": FakeResponse(503, b"down"),
        }
    )

    async def run():
        async with AsyncFetcher(
            policies={"example": policy(max_attempts=5, retry_budget=1)},
            user_agent="ai-intelligence-ingestion/0.2",
            session=session,
        ) as fetcher:
            return await fetcher.fetch("example", "https://example.com/data")

    result = asyncio.run(run())

    assert not result.ok
    # One request plus exactly one retry, not five.
    assert len([url for url in session.calls if url.endswith("/data")]) == 2


def test_circuit_breaker_opens_and_half_opens(monkeypatch):
    import ai_ingest.net as net

    clock = {"now": 0.0}
    monkeypatch.setattr(net.time, "monotonic", lambda: clock["now"])

    breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=10)
    breaker.record_failure()
    assert not breaker.is_open
    breaker.record_failure()
    assert breaker.is_open

    clock["now"] = 11.0
    assert not breaker.is_open


def test_invalid_policy_rejected():
    with pytest.raises(ValueError):
        SourcePolicy(name="x", domain="y", max_concurrency=100).validate()
