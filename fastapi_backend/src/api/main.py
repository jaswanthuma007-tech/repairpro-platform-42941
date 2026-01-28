import os
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


def _require_env(name: str) -> str:
    """Fetch a required environment variable or raise a clear startup error."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            "Ask the orchestrator to add it to this container's .env."
        )
    return value


SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

openapi_tags = [
    {
        "name": "System",
        "description": "Health checks and operational endpoints.",
    },
    {
        "name": "Repairs",
        "description": "Customer booking and tracking, technician status updates.",
    },
    {
        "name": "Admin",
        "description": "Administrative repair assignment and oversight.",
    },
]


app = FastAPI(
    title="RepairPro / Mobile Service Center API",
    description=(
        "Backend API for booking repairs and tracking repair status.\n\n"
        "Authentication: Supabase JWT (send `Authorization: Bearer <access_token>`).\n"
        "Authorization: Role-based via `profiles.role` (customer/technician/admin).\n"
    ),
    version="0.1.0",
    openapi_tags=openapi_tags,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, restrict to your frontend origin(s).
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class UserContext(BaseModel):
    """User identity and role derived from Supabase Auth + profiles table."""

    user_id: UUID = Field(..., description="Supabase auth user id (auth.users.id).")
    email: Optional[str] = Field(None, description="User email from Supabase auth.")
    role: str = Field(..., description="Application role: customer, technician, admin.")
    access_token: str = Field(..., description="Raw Supabase access token (JWT).")


class RepairCreateRequest(BaseModel):
    """Payload for creating a repair booking."""

    brand_id: UUID = Field(..., description="Selected brand id.")
    device_model_id: UUID = Field(..., description="Selected device model id.")
    service_id: UUID = Field(..., description="Requested service id.")
    issue_description: str = Field(..., min_length=5, description="Customer-reported issue.")
    address: str = Field(..., min_length=5, description="Pickup/dropoff address.")
    contact_phone: str = Field(..., min_length=6, description="Contact phone number.")


class RepairResponse(BaseModel):
    """Repair record returned to clients."""

    id: UUID = Field(..., description="Repair id.")
    status: str = Field(..., description="Current repair status.")
    customer_id: UUID = Field(..., description="Customer user id.")
    technician_id: Optional[UUID] = Field(None, description="Assigned technician user id.")
    brand_id: UUID = Field(..., description="Brand id.")
    device_model_id: UUID = Field(..., description="Device model id.")
    service_id: UUID = Field(..., description="Service id.")
    issue_description: str = Field(..., description="Issue description.")
    address: str = Field(..., description="Address.")
    contact_phone: str = Field(..., description="Contact phone.")
    created_at: Optional[datetime] = Field(None, description="Creation time.")
    updated_at: Optional[datetime] = Field(None, description="Last update time.")


class RepairStatusUpdateRequest(BaseModel):
    """Payload to update a repair's status."""

    status: str = Field(..., description="New status (e.g. pending, accepted, in_progress, completed, cancelled).")


class AssignTechnicianRequest(BaseModel):
    """Admin payload to assign a technician to a repair."""

    technician_id: UUID = Field(..., description="User id of technician to assign.")


class SupabaseRestClient:
    """Minimal Supabase PostgREST client with correct headers for RLS-aware access."""

    def __init__(self, *, supabase_url: str):
        if not supabase_url:
            raise RuntimeError(
                "SUPABASE_URL is not configured. Ask orchestrator to set SUPABASE_URL."
            )
        self._rest_base = f"{supabase_url}/rest/v1"

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
            # Supabase returns JSON with message/details on many errors.
            detail: Any
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise HTTPException(status_code=resp.status_code, detail=detail)
        if resp.status_code == status.HTTP_204_NO_CONTENT:
            return None
        # For PostgREST, successful responses are JSON arrays or objects.
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


_supabase = SupabaseRestClient(supabase_url=SUPABASE_URL) if SUPABASE_URL else None


def _postgrest_headers_from_user(token: str) -> Dict[str, str]:
    """
    Build headers for Supabase PostgREST with the user's JWT.
    Uses anon key as apikey (required by Supabase gateway) and JWT for RLS.
    """
    if not SUPABASE_ANON_KEY:
        raise RuntimeError(
            "SUPABASE_ANON_KEY is not configured. Ask orchestrator to set SUPABASE_ANON_KEY."
        )
    return {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _postgrest_headers_service_role() -> Dict[str, str]:
    """
    Build headers for Supabase PostgREST using the service role key.
    IMPORTANT: Only use server-side. This bypasses RLS, so we must enforce role checks in the API.
    """
    if not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY is not configured. Ask orchestrator to set SUPABASE_SERVICE_ROLE_KEY."
        )
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


async def _get_supabase_user(access_token: str) -> Dict[str, Any]:
    """Call Supabase Auth API to validate JWT and return user payload."""
    if not SUPABASE_URL:
        raise RuntimeError("SUPABASE_URL is not configured.")
    if not SUPABASE_ANON_KEY:
        raise RuntimeError("SUPABASE_ANON_KEY is not configured.")

    url = f"{SUPABASE_URL}/auth/v1/user"
    headers = {
        "apikey": SUPABASE_ANON_KEY,
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


async def _get_user_role(user_id: UUID, access_token: str) -> str:
    """
    Determine role from `public.profiles` using the user's JWT.
    If RLS prevents access or row is missing, default to 'customer' (per schema trigger default).
    """
    if _supabase is None:
        raise RuntimeError("Supabase client not initialized (missing SUPABASE_URL).")

    headers = _postgrest_headers_from_user(access_token)
    try:
        rows = await _supabase.select(
            "profiles",
            headers=headers,
            filters={"user_id": f"eq.{user_id}"},
            select="role",
            limit=1,
        )
        if isinstance(rows, list) and rows:
            role = rows[0].get("role")
            if isinstance(role, str) and role:
                return role
    except HTTPException:
        # If profile read fails due to RLS or missing row, keep safe default.
        pass
    return "customer"


# PUBLIC_INTERFACE
async def get_current_user(authorization: Optional[str] = Header(default=None)) -> UserContext:
    """FastAPI dependency: validate Supabase JWT and return user context (id/email/role)."""
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
    token = parts[1].strip()
    supa_user = await _get_supabase_user(token)
    user_id = UUID(supa_user["id"])
    role = await _get_user_role(user_id, token)
    return UserContext(
        user_id=user_id,
        email=supa_user.get("email"),
        role=role,
        access_token=token,
    )


def _require_role(user: UserContext, allowed: List[str]) -> None:
    """Raise 403 if user's role isn't permitted."""
    if user.role not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Insufficient role. Required one of: {allowed}.",
        )


# PUBLIC_INTERFACE
async def require_admin(user: UserContext = Depends(get_current_user)) -> UserContext:
    """FastAPI dependency: require admin role."""
    _require_role(user, ["admin"])
    return user


# PUBLIC_INTERFACE
async def require_technician_or_admin(user: UserContext = Depends(get_current_user)) -> UserContext:
    """FastAPI dependency: allow technician or admin."""
    _require_role(user, ["technician", "admin"])
    return user


@app.get(
    "/",
    tags=["System"],
    summary="Health check",
    description="Simple health check endpoint.",
    operation_id="health_check",
)
def health_check() -> Dict[str, str]:
    return {"message": "Healthy"}


@app.get(
    "/auth/me",
    tags=["System"],
    summary="Get current authenticated user",
    description="Returns the authenticated user's id/email/role derived from Supabase JWT and profiles table.",
    operation_id="get_auth_me",
)
async def auth_me(user: UserContext = Depends(get_current_user)) -> UserContext:
    return user


@app.post(
    "/repairs",
    tags=["Repairs"],
    summary="Create a repair booking (customer)",
    description=(
        "Creates a new repair booking owned by the authenticated customer.\n"
        "Uses Supabase RLS by inserting via PostgREST with the user's JWT."
    ),
    operation_id="create_repair",
    response_model=RepairResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_repair(
    payload: RepairCreateRequest,
    user: UserContext = Depends(get_current_user),
) -> RepairResponse:
    if _supabase is None:
        raise RuntimeError("Supabase client not initialized.")
    _require_role(user, ["customer", "admin"])  # admins can create on behalf if RLS allows; otherwise will fail.

    headers = _postgrest_headers_from_user(user.access_token)
    rows = await _supabase.insert(
        "repairs",
        headers=headers,
        rows=[
            {
                "customer_id": str(user.user_id),
                "technician_id": None,
                "brand_id": str(payload.brand_id),
                "device_model_id": str(payload.device_model_id),
                "service_id": str(payload.service_id),
                "issue_description": payload.issue_description,
                "address": payload.address,
                "contact_phone": payload.contact_phone,
                "status": "pending",
            }
        ],
    )
    if not isinstance(rows, list) or not rows:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Unexpected create response.")
    r = rows[0]
    return RepairResponse(**r)


@app.get(
    "/repairs/my",
    tags=["Repairs"],
    summary="List my repairs (customer)",
    description="Returns repairs visible to the authenticated user via Supabase RLS (typically their own repairs).",
    operation_id="list_my_repairs",
    response_model=List[RepairResponse],
)
async def list_my_repairs(user: UserContext = Depends(get_current_user)) -> List[RepairResponse]:
    if _supabase is None:
        raise RuntimeError("Supabase client not initialized.")
    headers = _postgrest_headers_from_user(user.access_token)

    # RLS will already scope results. We still add role-specific filters for efficiency/clarity.
    filters: Dict[str, str] = {}
    if user.role == "customer":
        filters["customer_id"] = f"eq.{user.user_id}"
    elif user.role == "technician":
        filters["technician_id"] = f"eq.{user.user_id}"

    rows = await _supabase.select("repairs", headers=headers, filters=filters, order="created_at.desc")
    if not isinstance(rows, list):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Unexpected list response.")
    return [RepairResponse(**r) for r in rows]


@app.patch(
    "/repairs/{repair_id}/status",
    tags=["Repairs"],
    summary="Update repair status (technician/admin)",
    description=(
        "Updates the status of a repair.\n"
        "Technicians can typically update only assigned repairs (enforced by RLS).\n"
        "Admins can update any repair (if RLS allows; otherwise use admin assignment endpoint with service role)."
    ),
    operation_id="update_repair_status",
    response_model=RepairResponse,
)
async def update_repair_status(
    repair_id: UUID,
    payload: RepairStatusUpdateRequest,
    user: UserContext = Depends(require_technician_or_admin),
) -> RepairResponse:
    if _supabase is None:
        raise RuntimeError("Supabase client not initialized.")
    headers = _postgrest_headers_from_user(user.access_token)

    rows = await _supabase.update(
        "repairs",
        headers=headers,
        filters={"id": f"eq.{repair_id}"},
        patch={"status": payload.status, "updated_at": datetime.utcnow().isoformat()},
    )
    if not isinstance(rows, list) or not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repair not found or not permitted by RLS.",
        )
    return RepairResponse(**rows[0])


@app.patch(
    "/admin/repairs/{repair_id}/assign",
    tags=["Admin"],
    summary="Assign technician to a repair (admin)",
    description=(
        "Admin-only endpoint to assign a technician.\n"
        "Uses Supabase service role key to bypass RLS (server-side enforcement of admin role is required)."
    ),
    operation_id="admin_assign_technician",
    response_model=RepairResponse,
)
async def admin_assign_technician(
    repair_id: UUID,
    payload: AssignTechnicianRequest,
    admin: UserContext = Depends(require_admin),
) -> RepairResponse:
    if _supabase is None:
        raise RuntimeError("Supabase client not initialized.")
    # Server enforces admin role via dependency; now perform privileged update.
    headers = _postgrest_headers_service_role()

    rows = await _supabase.update(
        "repairs",
        headers=headers,
        filters={"id": f"eq.{repair_id}"},
        patch={"technician_id": str(payload.technician_id), "updated_at": datetime.utcnow().isoformat()},
    )
    if not isinstance(rows, list) or not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repair not found.")
    return RepairResponse(**rows[0])
