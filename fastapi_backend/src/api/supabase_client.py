"""
Supabase REST (PostgREST) helper client for the FastAPI backend.

This module centralizes:
- A minimal PostgREST client wrapper (GET/POST/PATCH)
- Header builders for user-JWT requests (RLS-aware) and service-role requests (bypass RLS)

Environment variables (set in container .env by orchestrator):
- SUPABASE_URL
- SUPABASE_ANON_KEY (preferred)
- SUPABASE_KEY (legacy alias for SUPABASE_ANON_KEY; used by this repo's .env)
- SUPABASE_SERVICE_ROLE_KEY (only needed for admin/service endpoints)
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import httpx
from fastapi import HTTPException, status


def _require_env(name: str) -> str:
    """Fetch a required environment variable or raise a clear startup error."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            "Ask the orchestrator to add it to this container's .env."
        )
    return value


def _require_anon_key() -> str:
    """
    Return the Supabase anon key.

    This repo historically used SUPABASE_KEY in some environments; newer code expects
    SUPABASE_ANON_KEY. We accept either to avoid runtime 500s.
    """
    return os.getenv("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_KEY") or ""


def _supabase_url() -> str:
    return (os.getenv("SUPABASE_URL") or "").rstrip("/")


# PUBLIC_INTERFACE
def postgrest_headers_from_user(*, access_token: str) -> Dict[str, str]:
    """
    Build headers for Supabase PostgREST using the *user's* JWT (RLS-aware).

    Supabase requires:
    - `apikey`: anon key (gateway requirement)
    - `Authorization`: Bearer <user access token>
    """
    supabase_url = _supabase_url()
    if not supabase_url:
        raise RuntimeError("SUPABASE_URL is not configured. Ask orchestrator to set SUPABASE_URL.")
    anon_key = _require_anon_key()
    if not anon_key:
        raise RuntimeError(
            "Missing required environment variable: SUPABASE_ANON_KEY (or legacy SUPABASE_KEY). "
            "Ask the orchestrator to add it to this container's .env."
        )

    return {
        "apikey": anon_key,
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


# PUBLIC_INTERFACE
def postgrest_headers_service_role() -> Dict[str, str]:
    """
    Build headers for Supabase PostgREST using the service role key.

    IMPORTANT: This bypasses RLS. Only use server-side AND always enforce role checks in FastAPI.
    """
    supabase_url = _supabase_url()
    if not supabase_url:
        raise RuntimeError("SUPABASE_URL is not configured. Ask orchestrator to set SUPABASE_URL.")
    service_role_key = _require_env("SUPABASE_SERVICE_ROLE_KEY")

    return {
        "apikey": service_role_key,
        "Authorization": f"Bearer {service_role_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


class SupabaseRestClient:
    """Minimal Supabase PostgREST client with correct headers for RLS-aware access."""

    def __init__(self, *, supabase_url: str):
        if not supabase_url:
            raise RuntimeError("SUPABASE_URL is not configured. Ask orchestrator to set SUPABASE_URL.")
        self._rest_base = f"{supabase_url.rstrip('/')}/rest/v1"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        headers: Dict[str, str],
        params: Optional[Dict[str, Any]] = None,
        json: Any = None,
    ) -> Any:
        url = f"{self._rest_base}{path}"
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.request(method, url, headers=headers, params=params, json=json)

        if resp.status_code >= 400:
            try:
                detail: Any = resp.json()
            except Exception:
                detail = resp.text
            raise HTTPException(status_code=resp.status_code, detail=detail)

        if resp.status_code == status.HTTP_204_NO_CONTENT:
            return None

        try:
            return resp.json()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Unexpected response from Supabase: {e}",
            ) from e

    async def select(
        self,
        table: str,
        *,
        headers: Dict[str, str],
        filters: Optional[Dict[str, str]] = None,
        select: str = "*",
        order: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Any:
        params: Dict[str, Any] = {"select": select}
        if filters:
            params.update(filters)
        if order:
            params["order"] = order
        if limit is not None:
            params["limit"] = str(limit)
        return await self._request("GET", f"/{table}", headers=headers, params=params)

    async def insert(
        self,
        table: str,
        *,
        headers: Dict[str, str],
        rows: List[Dict[str, Any]],
        returning: str = "representation",
    ) -> Any:
        h = dict(headers)
        h["Prefer"] = f"return={returning}"
        return await self._request("POST", f"/{table}", headers=h, json=rows)

    async def update(
        self,
        table: str,
        *,
        headers: Dict[str, str],
        filters: Dict[str, str],
        patch: Dict[str, Any],
        returning: str = "representation",
    ) -> Any:
        params = dict(filters)
        h = dict(headers)
        h["Prefer"] = f"return={returning}"
        return await self._request("PATCH", f"/{table}", headers=h, params=params, json=patch)
