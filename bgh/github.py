import asyncio
import logging
import time

import httpx

from .config import gh_token

log = logging.getLogger("bgh.github")
API = "https://api.github.com"

# Background sync stops when a rate-limit bucket drops below this share of its hourly limit,
# leaving the rest for pages you open and for anything else using the same `gh` token.
RESERVE = 0.2
BUDGETED = ("graphql", "core")
# GitHub asks clients to wait at least a minute after a secondary (burst) rate limit.
SECONDARY_WAIT = 60


class GitHub:
    def __init__(self):
        self.client = httpx.AsyncClient(
            base_url=API,
            headers={
                "Authorization": f"bearer {gh_token()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=60,
            http2=False,
        )
        self.sem = asyncio.Semaphore(6)
        self.rate = {}  # resource ("graphql", "core", ...) -> (remaining, limit, reset epoch)
        self.paused_until = 0.0  # every request waits while we're rate limited

    @property
    def rate_remaining(self):
        return self.rate.get("graphql", (None,))[0]

    def _low(self):
        """[(resource, remaining, reset)] for budgeted buckets below the reserve."""
        now = time.time()
        return [(res, remaining, reset) for res, (remaining, limit, reset) in self.rate.items()
                if res in BUDGETED and remaining < limit * RESERVE and reset > now]

    def wait_note(self) -> str | None:
        """Human-readable reason requests are currently held back, if any."""
        now = time.time()
        if self.paused_until > now:
            return f"rate limited, resuming at {time.strftime('%H:%M', time.localtime(self.paused_until))}"
        for res, remaining, reset in self._low():
            return f"{res} budget low ({remaining} left), background sync resumes at {time.strftime('%H:%M', time.localtime(reset))}"
        return None

    async def wait_budget(self):
        """Background sync calls this before each unit of work; blocks while any bucket is below RESERVE."""
        while low := self._low():
            await asyncio.sleep(min(max(reset for _, _, reset in low) - time.time() + 1, 60))

    def _pause(self, seconds: float, why: str):
        until = time.time() + seconds
        if until > self.paused_until:
            self.paused_until = until
            log.warning("%s; pausing GitHub requests for %ds", why, seconds)

    def _track(self, r: httpx.Response):
        h = r.headers
        if all(k in h for k in ("x-ratelimit-remaining", "x-ratelimit-limit", "x-ratelimit-reset")):
            self.rate[h.get("x-ratelimit-resource", "core")] = (
                int(h["x-ratelimit-remaining"]), int(h["x-ratelimit-limit"]), int(h["x-ratelimit-reset"]))

    def _limited(self, r: httpx.Response) -> float | None:
        """Seconds to wait if this response is a rate limit, else None."""
        h = r.headers
        if r.status_code in (403, 429):
            if "retry-after" in h:
                return float(h["retry-after"])
            if h.get("x-ratelimit-remaining") == "0" and "x-ratelimit-reset" in h:
                return max(int(h["x-ratelimit-reset"]) - time.time(), 0) + 1
            if "rate limit" in r.text.lower():
                return SECONDARY_WAIT
        elif r.status_code == 200 and '"RATE_LIMITED"' in r.text and any(
                e.get("type") == "RATE_LIMITED" for e in r.json().get("errors") or []):
            # GraphQL reports an exhausted budget as a 200 with an error of type RATE_LIMITED.
            reset = int(h.get("x-ratelimit-reset", 0))
            return max(reset - time.time(), 0) + 1 if reset else SECONDARY_WAIT
        return None

    async def _request(self, method: str, path: str, **kw) -> httpx.Response:
        for attempt in range(6):
            while (wait := self.paused_until - time.time()) > 0:
                await asyncio.sleep(wait)
            async with self.sem:
                r = await self.client.request(method, path, **kw)
            self._track(r)
            if (wait := self._limited(r)) is not None:
                self._pause(wait, f"rate limited on {path} (HTTP {r.status_code})")
                continue
            if r.status_code in (502, 503, 504):
                await asyncio.sleep(2 ** attempt * 2)
                continue
            r.raise_for_status()
            return r
        r.raise_for_status()
        raise RuntimeError(f"GitHub request to {path} kept failing (HTTP {r.status_code})")

    async def graphql(self, query: str, **variables):
        r = await self._request("POST", "/graphql", json={"query": query, "variables": variables})
        data = r.json()
        if data.get("errors"):
            # Partial data (e.g. one missing blob) is fine; surface anything else.
            if not data.get("data"):
                raise RuntimeError(f"GraphQL error: {data['errors']}")
            log.debug("graphql partial errors: %s", data["errors"])
        return data["data"]

    async def rest_paginated(self, path: str, per_page=100, max_pages=30):
        out = []
        for page in range(1, max_pages + 1):
            batch = (await self._request("GET", path, params={"per_page": per_page, "page": page})).json()
            out.extend(batch)
            if len(batch) < per_page:
                break
        return out

    async def close(self):
        await self.client.aclose()
