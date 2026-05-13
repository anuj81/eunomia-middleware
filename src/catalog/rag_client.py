"""HTTP-backed RagClient — talks to the eunomia-rag service.

Sends the user's NLQ plus the OM-authorized view names to /v1/retrieve;
maps the response back to the OM-authoritative ViewInfo objects.
"""

from __future__ import annotations

import logging
from typing import List

import httpx

from ..config import Settings
from .base import ViewInfo
from .rag_base import RagClient, RagUnavailable

logger = logging.getLogger(__name__)


class HttpRagClient(RagClient):
    def __init__(self, settings: Settings):
        cfg = settings.rag
        self._url = cfg.url.rstrip("/")
        self._timeout = cfg.timeout_seconds
        self._api_key = cfg.api_key

    async def retrieve(
        self,
        query: str,
        allowed_views: List[ViewInfo],
        k: int,
    ) -> List[ViewInfo]:
        if not allowed_views:
            return []

        allowed_names = [v.name for v in allowed_views]
        headers: dict = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._url}/v1/retrieve",
                    headers=headers,
                    json={
                        "query": query,
                        "allowed_views": allowed_names,
                        "k": k,
                    },
                )
                resp.raise_for_status()
                body = resp.json()
        except httpx.HTTPError as e:
            logger.warning("RAG /v1/retrieve failed: %s", e)
            raise RagUnavailable(str(e)) from e

        # Map RAG-returned names back to OM-authoritative ViewInfo (preserves
        # OM's descriptions / column metadata; defends against stale RAG
        # payloads or any name not in the allowed list).
        by_name = {v.name: v for v in allowed_views}
        ranked: List[ViewInfo] = []
        unknown: List[str] = []
        for r in body.get("results") or []:
            name = r.get("name")
            if not name:
                continue
            if name in by_name:
                ranked.append(by_name[name])
            else:
                unknown.append(name)
        if unknown:
            logger.error(
                "RAG returned views outside the allowed set (dropped): %s",
                unknown,
            )
        return ranked
