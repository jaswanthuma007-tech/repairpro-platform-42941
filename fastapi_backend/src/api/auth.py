"""
Authentication and authorization utilities for the RepairPro FastAPI backend.

This module provides reusable FastAPI dependencies that:
- Validate a Supabase access token (JWT) using Supabase Auth `/auth/v1/user`
- Load the application's role from `public.profiles.role`
- Provide common role-guard dependencies for protecting endpoints

Environment variables (set in container .env by orchestrator):
- SUPABASE_URL
- SUPABASE_ANON_KEY (preferred)
- SUPABASE_KEY (legacy alias for SUPABASE_ANON_KEY; used by this repo's .env)
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional, Sequence
from uuid import UUID

import httpx
from fastapi import Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from src.api.supabase_client import SupabaseRestClient, postgrest_headers_from_user


def _require_supabase_env() -> tuple[str, str]:
    """Return (SUPABASE_URL, SUPABASE_ANON_KEY) or raise a clear configuration error."""
    supabase_url = (os.getenv("SUPABASE_URL") or "").rstrip("/")
    # Accept legacy SUPABASE_KEY to match this repo's .env and prevent runtime failures.
    anon_key = os.getenv("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_KEY") or ""
    if not supabase_url:
        raise RuntimeError(
            "SUPABASE_URL is not configured. Ask the orchestrator to set SUPABASE_URL in this container's .env."
        )
    if not anon_key:
        raise RuntimeError(
            "SUPABASE_ANON_KEY (or legacy SUPABASE_KEY) is not configured. "
            "Ask the orchestrator to set it in this container's .env."
        )
    return supabase_url, anon_key


class UserContext(BaseModel):
    """User identity and role derived from Supabase Auth + profiles table."""

    user_id: UUID = Field(..., description="Supabase auth user id (auth.users.id).")
    email: Optional[str] = Field(None, description="User email from Supabase auth.")
    role: str = Field(..., description="Application role: customer, technician, admin.")
    access_token: str = Field(..., description="Raw Supabase access token (JWT).")


async def _get_supabase_user(access_token: str) -> Dict[str, Any]:
    """Validate the Supabase JWT by calling Supabase Auth API and returning the user payload."""
    supabase_url, anon_key = _require_supabase_env()

    url = f"{supabase_url}/auth/v1/user"
    headers = {
        "apikey": anon_key,
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers=headers)

    if resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired Supabase access token.",
        )
    return resp.json()


async def _get_user_role(
    *,
    user_id: UUID,
    access_token: str,
    supabase: SupabaseRestClient,
) -> str:
    """
    Determine role from `public.profiles` using the user's JWT.

    If RLS prevents access or the row is missing, default to 'customer'
    (consistent with the SQL default/trigger behavior described in the project docs).
    """
    headers = postgrest_headers_from_user(access_token=access_token)
    try:
        rows = await supabase.select(
            "profiles",
            headers=headers,
            filters={"user_id": f"eq.{user_id}"},
            select="role",
            limit=1,
        )
        if isinstance(rows, list) and rows:
            role = rows[0].get("role")
            if isinstance(role, str) and role.strip():
                return role.strip()
    except HTTPException:
        # If profile read fails due to RLS or missing row, keep safe default.
        pass
    return "customer"


def _parse_bearer_token(authorization: Optional[str]) -> str:
    """Extract the bearer token from an Authorization header or raise 401."""
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header.",
        )

    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Authorization header format. Expected: Bearer <token>",
        )

    return parts[1].strip()


def require_roles(allowed_roles: Sequence[str]):
    """
    Create a dependency that requires the current user to have one of the allowed roles.

    Usage:
        @app.get("/admin/...", dependencies=[Depends(require_roles(["admin"]))])
        async def handler(user: UserContext = Depends(get_current_user)): ...
    """

    # PUBLIC_INTERFACE
    async def _require_roles_dep(user: UserContext = Depends(get_current_user)) -> UserContext:
        """FastAPI dependency: require that the current user has one of the allowed roles."""
        if user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Insufficient role. Required one of: {list(allowed_roles)}.",
            )
        return user

    return _require_roles_dep


# PUBLIC_INTERFACE
async def get_current_user(authorization: Optional[str] = Header(default=None)) -> UserContext:
    """
    FastAPI dependency: validate Supabase JWT and return user context (id/email/role).

    Parameters:
      - authorization: `Authorization: Bearer <access_token>`

    Returns:
      - UserContext: user_id, email, role, access_token
    """
    supabase_url, _anon_key = _require_supabase_env()
    token = _parse_bearer_token(authorization)

    supabase = SupabaseRestClient(supabase_url=supabase_url)
    supa_user = await _get_supabase_user(token)

    user_id = UUID(supa_user["id"])
    role = await _get_user_role(user_id=user_id, access_token=token, supabase=supabase)

    return UserContext(
        user_id=user_id,
        email=supa_user.get("email"),
        role=role,
        access_token=token,
    )


# PUBLIC_INTERFACE
async def require_authenticated(user: UserContext = Depends(get_current_user)) -> UserContext:
    """FastAPI dependency: any authenticated user."""
    return user


# PUBLIC_INTERFACE
async def require_admin(user: UserContext = Depends(get_current_user)) -> UserContext:
    """FastAPI dependency: require admin role."""
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient role. Required one of: ['admin'].",
        )
    return user


# PUBLIC_INTERFACE
async def require_technician_or_admin(user: UserContext = Depends(get_current_user)) -> UserContext:
    """FastAPI dependency: allow technician or admin."""
    if user.role not in ("technician", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient role. Required one of: ['technician', 'admin'].",
        )
    return user
