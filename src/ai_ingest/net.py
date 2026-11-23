"""Bounded, policy-aware asynchronous HTTP acquisition.

Design constraints (Phase V):
- asyncio + aiohttp only; no browser automation for static/structured sources.
- per-domain concurrency caps and minimum spacing.
- robots.txt checked before any page fetch (feeds and documented APIs are
  queried directly, but every article/HTML fetch is gated).
- exponential backoff with jitter, Retry-After support and a retry budget.
- per-source circuit breaker and full telemetry for the run report.
- no captcha solving, no proxy rotation, no header spoofing.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import aiohttp


@dataclass(frozen=True)
class SourcePolicy:
    name: str
    domain: str
    max_concurrency: int = 4
    min_interval: float = 0.5
    max_attempts: int = 4
    retry_budget: int = 3
    respect_robots: bool = True
    max_bytes: int = 4_000_000

    def validate(self) -> None:
        if not self.name.strip() or not self.domain.strip():
            raise ValueError("Policy name and domain are required.")
        if not 1 <= self.max_concurrency <= 16:
            raise ValueError("max_concurrency must be between 1 and 16.")
        if self.min_interval < 0 or self.retry_budget < 0:
            raise ValueError("Intervals and budgets cannot be negative.")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive.")


@dataclass
class SourceMetrics:
    requests: int = 0
    successes: int = 0
    failures: int = 0
    retries: int = 0
    rate_limit_events: int = 0
    blocked_by_robots: int = 0
    circuit_open: int = 0
    bytes_received: int = 0
    elapsed_seconds: float = 0.0

    def as_dict(self) -> dict:
        return {
            "requests": self.requests,
            "successes": self.successes,
            "failures": self.failures,
            "retries": self.retries,
            "rate_limit_events": self.rate_limit_events,
            "blocked_by_robots": self.blocked_by_robots,
            "circuit_open": self.circuit_open,
            "bytes_received": self.bytes_received,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
        }


@dataclass
class FetchResult:
    source: str
    url: str
    status: int | None
    body: bytes
    content_type: str
    attempts: int
    retries: int
    from_cache: bool = False
    blocked_reason: str | None = None
    saved_path: str | None = None
    raw_sha256: str | None = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def ok(self) -> bool:
        return (
            self.status is not None
            and 200 <= self.status < 300
            and bool(self.body)
        )

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


class CircuitBreaker:
    """Per-source breaker: opens after repeated failures, then half-opens."""

    def __init__(self, *, failure_threshold: int = 5, cooldown_seconds: float = 60.0) -> None:
        if failure_threshold < 1 or cooldown_seconds < 0:
            raise ValueError("Invalid circuit breaker configuration.")
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self.cooldown_seconds:
            # Half-open: allow one probe.
            self._opened_at = None
            self._failures = 0
            return False
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._opened_at = time.monotonic()


class AsyncFetcher:
    def __init__(
        self,
        *,
        policies: dict[str, SourcePolicy],
        user_agent: str,
        raw_dir: Path | None = None,
        session: aiohttp.ClientSession | None = None,
        rng: random.Random | None = None,
    ) -> None:
        if not user_agent.strip():
            raise ValueError("A descriptive User-Agent is required.")

        for policy in policies.values():
            policy.validate()

        self.policies = policies
        self.user_agent = user_agent
        self.raw_dir = raw_dir
        self.session = session
        self._owns_session = session is None
        self._rng = rng or random.Random(0xA11CE)

        self._semaphores = {
            name: asyncio.Semaphore(policy.max_concurrency)
            for name, policy in policies.items()
        }
        self._domain_locks = {
            policy.domain: asyncio.Lock() for policy in policies.values()
        }
        self._domain_last = {policy.domain: 0.0 for policy in policies.values()}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}
        self._circuits = {name: CircuitBreaker() for name in policies}
        self.metrics = {name: SourceMetrics() for name in policies}
        self.events: list[dict] = []

        if raw_dir is not None:
            raw_dir.mkdir(parents=True, exist_ok=True)

    async def __aenter__(self) -> "AsyncFetcher":
        if self.session is None:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=45),
                headers={"User-Agent": self.user_agent},
                connector=aiohttp.TCPConnector(limit=32, limit_per_host=6),
            )
        return self

    async def __aexit__(self, *_exc) -> None:
        if self._owns_session and self.session is not None:
            await self.session.close()
            self.session = None

    def _log(self, **event: object) -> None:
        payload = {
            "at": datetime.now(timezone.utc).isoformat(),
            **event,
        }
        self.events.append(payload)

    async def _robots_for(self, origin: str, policy: SourcePolicy) -> RobotFileParser | None:
        if origin in self._robots:
            return self._robots[origin]

        lock = self._robots_locks.setdefault(origin, asyncio.Lock())
        async with lock:
            if origin in self._robots:
                return self._robots[origin]

            assert self.session is not None
            parser: RobotFileParser | None = None
            try:
                async with self.session.get(
                    f"{origin}/robots.txt",
                    allow_redirects=True,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as response:
                    if response.status == 200:
                        body = await response.text()
                        parser = RobotFileParser()
                        parser.parse(body.splitlines())
                        self._log(
                            event="robots_loaded",
                            source=policy.name,
                            origin=origin,
                            status=200,
                        )
                    elif response.status in {401, 403}:
                        parser = None
                        self._log(
                            event="robots_denied",
                            source=policy.name,
                            origin=origin,
                            status=response.status,
                        )
                    else:
                        # 404/410 means no restrictions published.
                        parser = RobotFileParser()
                        parser.parse([])
            except (aiohttp.ClientError, asyncio.TimeoutError):
                # Conservative: no confirmed policy, so do not scrape HTML.
                parser = None
                self._log(
                    event="robots_unavailable",
                    source=policy.name,
                    origin=origin,
                )

            self._robots[origin] = parser
            return parser

    async def _allowed(self, url: str, policy: SourcePolicy) -> bool:
        if not policy.respect_robots:
            return True

        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        parser = await self._robots_for(origin, policy)

        if parser is None:
            return False

        return parser.can_fetch(self.user_agent, url)

    async def _wait_for_slot(self, policy: SourcePolicy) -> None:
        lock = self._domain_locks[policy.domain]
        async with lock:
            elapsed = time.monotonic() - self._domain_last[policy.domain]
            if elapsed < policy.min_interval:
                await asyncio.sleep(policy.min_interval - elapsed)
            self._domain_last[policy.domain] = time.monotonic()

    def _jitter(self, attempt: int) -> float:
        return self._rng.uniform(0.0, min(30.0, 2.0 ** attempt))

    @staticmethod
    def _retry_after(response: aiohttp.ClientResponse) -> float | None:
        value = response.headers.get("Retry-After")
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            return None

    async def fetch(
        self,
        source: str,
        url: str,
        *,
        cache_key: str | None = None,
        check_robots: bool = True,
    ) -> FetchResult:
        policy = self.policies.get(source)
        if policy is None:
            raise ValueError(f"Unknown source policy: {source}")

        if self.session is None:
            raise RuntimeError("Use AsyncFetcher as an async context manager.")

        started = time.monotonic()
        semaphore = self._semaphores[source]
        metrics = self.metrics[source]
        circuit = self._circuits[source]

        if circuit.is_open:
            metrics.circuit_open += 1
            self._log(event="circuit_open", source=source, url=url)
            return FetchResult(
                source=source,
                url=url,
                status=None,
                body=b"",
                content_type="",
                attempts=0,
                retries=0,
                blocked_reason="CIRCUIT_OPEN",
            )

        if check_robots and not await self._allowed(url, policy):
            metrics.blocked_by_robots += 1
            self._log(event="robots_disallowed", source=source, url=url)
            return FetchResult(
                source=source,
                url=url,
                status=None,
                body=b"",
                content_type="",
                attempts=0,
                retries=0,
                blocked_reason="ROBOTS_DISALLOWED",
            )

        retries = 0
        last_status: int | None = None
        last_error: str | None = None
        body = b""
        content_type = ""

        for attempt in range(policy.max_attempts):
            async with semaphore:
                await self._wait_for_slot(policy)
                metrics.requests += 1
                attempt_started = time.monotonic()

                try:
                    async with self.session.get(
                        url,
                        allow_redirects=True,
                        timeout=aiohttp.ClientTimeout(total=45),
                    ) as response:
                        last_status = response.status
                        content_type = response.headers.get("Content-Type", "")

                        if response.status == 200:
                            accumulated = bytearray()
                            async for chunk in response.content.iter_chunked(65_536):
                                accumulated.extend(chunk)
                                if len(accumulated) > policy.max_bytes:
                                    raise ValueError(
                                        "Response exceeded the byte budget."
                                    )
                            body = bytes(accumulated)

                            metrics.successes += 1
                            metrics.bytes_received += len(body)
                            circuit.record_success()

                            raw_sha256 = hashlib.sha256(body).hexdigest()
                            saved_path = None
                            if self.raw_dir is not None:
                                key = cache_key or raw_sha256[:16]
                                saved = self.raw_dir / f"{source}-{key}.bin"
                                saved.write_bytes(body)
                                saved_path = str(saved)

                            metrics.elapsed_seconds += time.monotonic() - started
                            self._log(
                                event="request",
                                source=source,
                                url=url,
                                status=200,
                                attempt=attempt + 1,
                                latency_ms=round(
                                    (time.monotonic() - attempt_started) * 1000, 1
                                ),
                                outcome="success",
                            )
                            return FetchResult(
                                source=source,
                                url=url,
                                status=200,
                                body=body,
                                content_type=content_type,
                                attempts=attempt + 1,
                                retries=retries,
                                saved_path=saved_path,
                                raw_sha256=raw_sha256,
                            )

                        retry_after = self._retry_after(response)
                        snippet = await response.text()
                        last_error = f"HTTP {response.status}: {snippet[:160]}"

                        if response.status == 429:
                            metrics.rate_limit_events += 1

                        retryable = response.status in {429, 500, 502, 503, 504}
                        self._log(
                            event="request",
                            source=source,
                            url=url,
                            status=response.status,
                            attempt=attempt + 1,
                            latency_ms=round(
                                (time.monotonic() - attempt_started) * 1000, 1
                            ),
                            outcome=(
                                "rate_limited"
                                if response.status == 429
                                else "retryable"
                                if retryable
                                else "failed"
                            ),
                        )

                        if not retryable:
                            metrics.failures += 1
                            circuit.record_failure()
                            metrics.elapsed_seconds += time.monotonic() - started
                            return FetchResult(
                                source=source,
                                url=url,
                                status=response.status,
                                body=b"",
                                content_type=content_type,
                                attempts=attempt + 1,
                                retries=retries,
                                blocked_reason=last_error,
                            )

                        if retries >= policy.retry_budget:
                            break

                        retries += 1
                        metrics.retries += 1
                        await asyncio.sleep(
                            max(retry_after or 0.0, self._jitter(attempt))
                        )

                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    metrics.failures += 1
                    circuit.record_failure()
                    self._log(
                        event="request_error",
                        source=source,
                        url=url,
                        attempt=attempt + 1,
                        error=last_error,
                    )
                    if retries >= policy.retry_budget:
                        break
                    retries += 1
                    metrics.retries += 1
                    await asyncio.sleep(self._jitter(attempt))

        metrics.failures += 1
        metrics.elapsed_seconds += time.monotonic() - started
        return FetchResult(
            source=source,
            url=url,
            status=last_status,
            body=b"",
            content_type=content_type,
            attempts=policy.max_attempts,
            retries=retries,
            blocked_reason=last_error or "RETRY_BUDGET_EXHAUSTED",
        )

    def report(self) -> dict:
        return {
            "user_agent": self.user_agent,
            "policy": {
                name: {
                    "domain": policy.domain,
                    "max_concurrency": policy.max_concurrency,
                    "min_interval_seconds": policy.min_interval,
                    "max_attempts": policy.max_attempts,
                    "retry_budget": policy.retry_budget,
                    "respect_robots": policy.respect_robots,
                    "max_bytes": policy.max_bytes,
                }
                for name, policy in self.policies.items()
            },
            "metrics": {
                name: metric.as_dict() for name, metric in self.metrics.items()
            },
            "max_concurrent_requests": max(
                (policy.max_concurrency for policy in self.policies.values()),
                default=0,
            ),
            "events": self.events,
        }

    def save_report(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.report(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
