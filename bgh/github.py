import asyncio
import logging

import httpx

from .config import gh_token

log = logging.getLogger("bgh.github")
API = "https://api.github.com"


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
        self.rate_remaining = None

    async def graphql(self, query: str, **variables):
        async with self.sem:
            for attempt in range(4):
                r = await self.client.post("/graphql", json={"query": query, "variables": variables})
                if r.status_code in (502, 503, 504) or (r.status_code == 403 and "rate" in r.text.lower()):
                    await asyncio.sleep(2 ** attempt * 2)
                    continue
                r.raise_for_status()
                data = r.json()
                if data.get("errors"):
                    # Partial data (e.g. one missing blob) is fine; surface anything else.
                    if not data.get("data"):
                        raise RuntimeError(f"GraphQL error: {data['errors']}")
                    log.debug("graphql partial errors: %s", data["errors"])
                rl = (data.get("data") or {}).get("rateLimit")
                if rl:
                    self.rate_remaining = rl["remaining"]
                return data["data"]
            r.raise_for_status()

    async def rest_paginated(self, path: str, per_page=100, max_pages=30):
        out = []
        async with self.sem:
            for page in range(1, max_pages + 1):
                r = await self.client.get(path, params={"per_page": per_page, "page": page})
                r.raise_for_status()
                self.rate_remaining = r.headers.get("x-ratelimit-remaining", self.rate_remaining)
                batch = r.json()
                out.extend(batch)
                if len(batch) < per_page:
                    break
        return out

    async def close(self):
        await self.client.aclose()
