import os
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Set
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from src.api.auth import (
    UserContext,
    get_current_user,
    require_admin,
    require_authenticated,
    require_technician_or_admin,
)
from src.api.supabase_client import (
    SupabaseRestClient,
    postgrest_headers_from_user,
    postgrest_headers_service_role,
)


def _split_origins(raw: str) -> List[str]:
    """Parse a comma-separated list of CORS origins into a clean list."""
    return [o.strip() for o in (raw or "").split(",") if o.strip()]


# Supabase config (required for authenticated endpoints).
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")

# CORS:
# - In production, set CORS_ALLOW_ORIGINS to your deployed frontend URL(s).
# - Comma-separated, e.g.: "http://localhost:3000,https://app.example.com"
# - If unset, the API defaults to localhost:3000 origins for development.
CORS_ALLOW_ORIGINS = os.getenv("CORS_ALLOW_ORIGINS", "")

openapi_tags = [
    {"name": "System", "description": "Health checks and operational endpoints."},
    {
        "name": "Catalog",
        "description": "Read-only catalog data (brands, device models, services) used by booking flow.",
    },
    {"name": "Repairs", "description": "Customer booking and tracking, technician status updates."},
    {"name": "Admin", "description": "Administrative repair assignment and catalog management."},
]

app = FastAPI(
    title="RepairPro / Mobile Service Center API",
    description=(
        "Backend API for booking repairs and tracking repair status.\n\n"
        "Authentication: Supabase JWT (send `Authorization: Bearer <access_token>`).\n"
        "Authorization: Role-based via `profiles.role` (customer/technician/admin).\n\n"
        "Note: Most data access relies on Supabase Row Level Security (RLS). "
        "Admin-only endpoints may use the Supabase service role key server-side to bypass RLS, "
        "and therefore MUST enforce admin role in this API.\n"
    ),
    version="0.1.0",
    openapi_tags=openapi_tags,
)

# CORS policy: default to safe development origins if CORS_ALLOW_ORIGINS is unset.
_default_dev_origins = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]
_allowed_origins = _split_origins(CORS_ALLOW_ORIGINS) or _default_dev_origins

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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

    status: str = Field(
        ...,
        description="New status (pending, accepted, in_progress, completed, cancelled).",
    )


class AdminRepairAnalyticsResponse(BaseModel):
    """Admin analytics summary across repairs."""

    total_repairs: int = Field(..., description="Total repairs in the system.")
    by_status: Dict[str, int] = Field(..., description="Counts of repairs grouped by status.")
    by_day_last_14d: List[Dict[str, Any]] = Field(
        ...,
        description=("Daily counts for last 14 days. Each item contains: {date: 'YYYY-MM-DD', count: int}."),
    )


class AssignTechnicianRequest(BaseModel):
    """Admin payload to assign a technician to a repair."""

    technician_id: UUID = Field(..., description="User id of technician to assign.")


class BrandResponse(BaseModel):
    """Brand record for booking catalog."""

    id: UUID = Field(..., description="Brand id.")
    name: str = Field(..., description="Brand name.")


class BrandCreateRequest(BaseModel):
    """Admin payload to create a brand."""

    name: str = Field(..., min_length=2, description="Brand name.")


class DeviceModelResponse(BaseModel):
    """Device model record for booking catalog."""

    id: UUID = Field(..., description="Device model id.")
    brand_id: UUID = Field(..., description="Brand id this model belongs to.")
    name: str = Field(..., description="Device model name.")


class DeviceModelCreateRequest(BaseModel):
    """Admin payload to create a device model under a brand."""

    brand_id: UUID = Field(..., description="Brand id this model belongs to.")
    name: str = Field(..., min_length=2, description="Device model name.")


class ServiceResponse(BaseModel):
    """Service record for booking catalog."""

    id: UUID = Field(..., description="Service id.")
    name: str = Field(..., description="Service name.")
    base_price: Optional[float] = Field(None, description="Base price (informational).")


class ServiceCreateRequest(BaseModel):
    """Admin payload to create a service."""

    name: str = Field(..., min_length=2, description="Service name.")
    base_price: Optional[float] = Field(None, ge=0, description="Base price (informational).")


class RepairStatusHistoryResponse(BaseModel):
    """Repair status history record."""

    id: UUID = Field(..., description="History record id.")
    repair_id: UUID = Field(..., description="Linked repair id.")
    status: str = Field(..., description="Status value at this point in time.")
    created_at: Optional[datetime] = Field(None, description="Timestamp of status change.")


# Allowed statuses should match DB constraint/enum (kept here to provide clear API errors).
_ALLOWED_REPAIR_STATUSES: Set[str] = {
    "pending",
    "accepted",
    "in_progress",
    "completed",
    "cancelled",
}

# Business transitions to prevent invalid state changes from API clients.
# Note: DB can still be the ultimate authority; this is API hardening for clearer errors.
_ALLOWED_TRANSITIONS: Dict[str, Set[str]] = {
    "pending": {"accepted", "cancelled"},
    "accepted": {"in_progress", "cancelled"},
    "in_progress": {"completed", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}


def _validate_repair_status_value(status_value: str) -> None:
    """Validate status value and raise a 422 with a helpful message if invalid."""
    if status_value not in _ALLOWED_REPAIR_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": "Invalid status value.",
                "allowed": sorted(_ALLOWED_REPAIR_STATUSES),
                "received": status_value,
            },
        )


def _require_role(user: UserContext, allowed: List[str]) -> None:
    """Raise 403 if user's role isn't permitted."""
    if user.role not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Insufficient role. Required one of: {allowed}.",
        )


def _validate_repair_status_update_permissions(user: UserContext, new_status: str) -> None:
    """
    Enforce basic business rules by role.

    Note: RLS remains the source of truth for *which* repair rows a user can modify.
    This function enforces *what* transitions/values are permitted by role.
    """
    # Technicians/admin can update status, but technician shouldn't set "cancelled" (customer/admin action).
    if user.role == "technician" and new_status == "cancelled":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Technicians cannot set status to 'cancelled'.",
        )


_supabase = SupabaseRestClient(supabase_url=SUPABASE_URL) if SUPABASE_URL else None


async def _get_repair_current_status(repair_id: UUID, *, access_token: str) -> Optional[str]:
    """Fetch current status of a repair using caller JWT (RLS-scoped)."""
    if _supabase is None:
        raise RuntimeError("Supabase client not initialized.")
    headers = postgrest_headers_from_user(access_token=access_token)
    rows = await _supabase.select(
        "repairs",
        headers=headers,
        filters={"id": f"eq.{repair_id}"},
        select="status",
        limit=1,
    )
    if not isinstance(rows, list):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Unexpected response.")
    if not rows:
        return None
    st = rows[0].get("status")
    return st if isinstance(st, str) else None


def _validate_transition(old_status: str, new_status: str) -> None:
    """Validate status transition; raise 409 for disallowed transitions."""
    allowed = _ALLOWED_TRANSITIONS.get(old_status)
    if allowed is None:
        # If DB contains unexpected status, treat as conflict for safety.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot transition from unknown status '{old_status}'.",
        )
    if new_status not in allowed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Invalid status transition.",
                "from": old_status,
                "to": new_status,
                "allowed_next": sorted(list(allowed)),
            },
        )


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


@app.get(
    "/docs/auth",
    tags=["System"],
    summary="Auth and role usage guide",
    description="Helper endpoint describing how to call this API with Supabase JWT and how roles are enforced.",
    operation_id="docs_auth_guide",
)
def docs_auth_guide() -> Dict[str, Any]:
    return {
        "authentication": {
            "type": "Supabase JWT",
            "header": "Authorization: Bearer <access_token>",
            "how_to_get_token": "Use Supabase Auth on the frontend to sign in; use the returned access_token.",
        },
        "authorization": {
            "roles": ["customer", "technician", "admin"],
            "source": "public.profiles.role (looked up using the user's JWT under RLS).",
            "service_role_note": (
                "Admin endpoints may use SUPABASE_SERVICE_ROLE_KEY server-side to bypass RLS; "
                "API strictly enforces admin role."
            ),
        },
        "cors": {
            "allow_origins": _allowed_origins,
            "note": "Configure CORS_ALLOW_ORIGINS as a comma-separated list for production.",
        },
        "required_env": {
            "SUPABASE_URL": "Supabase project URL",
            "SUPABASE_ANON_KEY": "Supabase anon key (safe for clients; used here for gateway)",
            "SUPABASE_SERVICE_ROLE_KEY": "Backend-only service role key (admin endpoints)",
            "CORS_ALLOW_ORIGINS": "Optional. Comma-separated origins. Defaults to localhost:3000 in dev.",
        },
    }


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
    _require_role(user, ["customer", "admin"])  # admin allowed if RLS permits; otherwise will fail.

    headers = postgrest_headers_from_user(access_token=user.access_token)
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
    return RepairResponse(**rows[0])


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
    headers = postgrest_headers_from_user(access_token=user.access_token)

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


@app.get(
    "/repairs/{repair_id}",
    tags=["Repairs"],
    summary="Get repair by id (RLS-scoped)",
    description=(
        "Returns a single repair by id if visible to the current user under Supabase RLS.\n"
        "Customers typically see their own repairs; technicians see assigned; admins see all."
    ),
    operation_id="get_repair",
    response_model=RepairResponse,
)
async def get_repair(repair_id: UUID, user: UserContext = Depends(require_authenticated)) -> RepairResponse:
    if _supabase is None:
        raise RuntimeError("Supabase client not initialized.")
    headers = postgrest_headers_from_user(access_token=user.access_token)

    rows = await _supabase.select(
        "repairs",
        headers=headers,
        filters={"id": f"eq.{repair_id}"},
        limit=1,
    )
    if not isinstance(rows, list):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Unexpected response.")
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repair not found or not permitted by RLS.",
        )
    return RepairResponse(**rows[0])


@app.get(
    "/repairs/{repair_id}/history",
    tags=["Repairs"],
    summary="Get repair status history (RLS-scoped)",
    description=(
        "Returns status history for a repair.\n"
        "Visibility is enforced by RLS: only users who can see the repair can see its history."
    ),
    operation_id="get_repair_history",
    response_model=List[RepairStatusHistoryResponse],
)
async def get_repair_history(
    repair_id: UUID,
    user: UserContext = Depends(require_authenticated),
) -> List[RepairStatusHistoryResponse]:
    if _supabase is None:
        raise RuntimeError("Supabase client not initialized.")
    headers = postgrest_headers_from_user(access_token=user.access_token)

    rows = await _supabase.select(
        "repair_status_history",
        headers=headers,
        filters={"repair_id": f"eq.{repair_id}"},
        order="created_at.desc",
    )
    if not isinstance(rows, list):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Unexpected response.")
    return [RepairStatusHistoryResponse(**r) for r in rows]


@app.get(
    "/repairs/jobs",
    tags=["Repairs"],
    summary="List repairs for technician/admin",
    description=(
        "Technician/admin endpoint to list job repairs.\n"
        "Technicians will be scoped to assigned jobs by RLS; admins may see all."
    ),
    operation_id="list_jobs",
    response_model=List[RepairResponse],
)
async def list_jobs(
    user: UserContext = Depends(require_technician_or_admin),
    status_filter: Optional[str] = Query(default=None, description="Optional status filter (exact match)."),
) -> List[RepairResponse]:
    if _supabase is None:
        raise RuntimeError("Supabase client not initialized.")
    headers = postgrest_headers_from_user(access_token=user.access_token)

    filters: Dict[str, str] = {}
    if user.role == "technician":
        filters["technician_id"] = f"eq.{user.user_id}"
    if status_filter:
        filters["status"] = f"eq.{status_filter}"

    rows = await _supabase.select("repairs", headers=headers, filters=filters, order="created_at.desc")
    if not isinstance(rows, list):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Unexpected response.")
    return [RepairResponse(**r) for r in rows]


@app.patch(
    "/repairs/{repair_id}/status",
    tags=["Repairs"],
    summary="Update repair status (technician/admin)",
    description=(
        "Updates the status of a repair.\n"
        "Technicians can typically update only assigned repairs (enforced by RLS).\n"
        "Admins can update any repair (if RLS allows; otherwise use admin assignment endpoint with service role).\n\n"
        "Server-side validation:\n"
        "- status must be one of the allowed values\n"
        "- transition must be valid (e.g. pending -> accepted -> in_progress -> completed)\n"
        "- technicians cannot set status to 'cancelled'\n"
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

    _validate_repair_status_value(payload.status)
    _validate_repair_status_update_permissions(user, payload.status)

    # Fetch current status (RLS-scoped) to validate transition and provide clearer errors.
    current_status = await _get_repair_current_status(repair_id, access_token=user.access_token)
    if current_status is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repair not found or not permitted by RLS.",
        )
    if current_status == payload.status:
        # Idempotent update: return current repair state by re-selecting full row.
        return await get_repair(repair_id, user=user)

    _validate_transition(current_status, payload.status)

    headers = postgrest_headers_from_user(access_token=user.access_token)

    # Let DB triggers manage updated_at and history logging.
    rows = await _supabase.update(
        "repairs",
        headers=headers,
        filters={"id": f"eq.{repair_id}"},
        patch={"status": payload.status},
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
    headers = postgrest_headers_service_role()

    # Do NOT override updated_at in API; rely on DB triggers/defaults for consistency.
    rows = await _supabase.update(
        "repairs",
        headers=headers,
        filters={"id": f"eq.{repair_id}"},
        patch={"technician_id": str(payload.technician_id)},
    )
    if not isinstance(rows, list) or not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repair not found.")
    return RepairResponse(**rows[0])


@app.get(
    "/brands",
    tags=["Catalog"],
    summary="List brands",
    description="Returns the list of brands (authenticated users; RLS enforced by Supabase).",
    operation_id="list_brands",
    response_model=List[BrandResponse],
)
async def list_brands(user: UserContext = Depends(require_authenticated)) -> List[BrandResponse]:
    if _supabase is None:
        raise RuntimeError("Supabase client not initialized.")
    headers = postgrest_headers_from_user(access_token=user.access_token)
    rows = await _supabase.select("brands", headers=headers, order="name.asc")
    if not isinstance(rows, list):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Unexpected response.")
    return [BrandResponse(**r) for r in rows]


@app.get(
    "/device-models",
    tags=["Catalog"],
    summary="List device models",
    description="Returns device models, optionally filtered by brand_id (authenticated users; RLS enforced by Supabase).",
    operation_id="list_device_models",
    response_model=List[DeviceModelResponse],
)
async def list_device_models(
    user: UserContext = Depends(require_authenticated),
    brand_id: Optional[UUID] = Query(default=None, description="Optional brand id filter."),
) -> List[DeviceModelResponse]:
    if _supabase is None:
        raise RuntimeError("Supabase client not initialized.")
    headers = postgrest_headers_from_user(access_token=user.access_token)

    filters: Dict[str, str] = {}
    if brand_id:
        filters["brand_id"] = f"eq.{brand_id}"

    rows = await _supabase.select("device_models", headers=headers, filters=filters, order="name.asc")
    if not isinstance(rows, list):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Unexpected response.")
    return [DeviceModelResponse(**r) for r in rows]


@app.get(
    "/services",
    tags=["Catalog"],
    summary="List services",
    description="Returns services (authenticated users; RLS enforced by Supabase).",
    operation_id="list_services",
    response_model=List[ServiceResponse],
)
async def list_services(user: UserContext = Depends(require_authenticated)) -> List[ServiceResponse]:
    if _supabase is None:
        raise RuntimeError("Supabase client not initialized.")
    headers = postgrest_headers_from_user(access_token=user.access_token)

    rows = await _supabase.select("services", headers=headers, order="name.asc")
    if not isinstance(rows, list):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Unexpected response.")
    return [ServiceResponse(**r) for r in rows]


@app.post(
    "/admin/brands",
    tags=["Admin"],
    summary="Create brand (admin)",
    description="Admin-only: create a new brand using service role key (bypasses RLS).",
    operation_id="admin_create_brand",
    response_model=BrandResponse,
    status_code=status.HTTP_201_CREATED,
)
async def admin_create_brand(
    payload: BrandCreateRequest,
    admin: UserContext = Depends(require_admin),
) -> BrandResponse:
    if _supabase is None:
        raise RuntimeError("Supabase client not initialized.")
    headers = postgrest_headers_service_role()
    rows = await _supabase.insert("brands", headers=headers, rows=[{"name": payload.name}])
    if not isinstance(rows, list) or not rows:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Unexpected create response.")
    return BrandResponse(**rows[0])


@app.post(
    "/admin/device-models",
    tags=["Admin"],
    summary="Create device model (admin)",
    description="Admin-only: create a new device model under a brand using service role key (bypasses RLS).",
    operation_id="admin_create_device_model",
    response_model=DeviceModelResponse,
    status_code=status.HTTP_201_CREATED,
)
async def admin_create_device_model(
    payload: DeviceModelCreateRequest,
    admin: UserContext = Depends(require_admin),
) -> DeviceModelResponse:
    if _supabase is None:
        raise RuntimeError("Supabase client not initialized.")
    headers = postgrest_headers_service_role()
    rows = await _supabase.insert(
        "device_models",
        headers=headers,
        rows=[{"brand_id": str(payload.brand_id), "name": payload.name}],
    )
    if not isinstance(rows, list) or not rows:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Unexpected create response.")
    return DeviceModelResponse(**rows[0])


@app.post(
    "/admin/services",
    tags=["Admin"],
    summary="Create service (admin)",
    description="Admin-only: create a new service using service role key (bypasses RLS).",
    operation_id="admin_create_service",
    response_model=ServiceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def admin_create_service(
    payload: ServiceCreateRequest,
    admin: UserContext = Depends(require_admin),
) -> ServiceResponse:
    if _supabase is None:
        raise RuntimeError("Supabase client not initialized.")
    headers = postgrest_headers_service_role()
    rows = await _supabase.insert(
        "services",
        headers=headers,
        rows=[{"name": payload.name, "base_price": payload.base_price}],
    )
    if not isinstance(rows, list) or not rows:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Unexpected create response.")
    return ServiceResponse(**rows[0])


def _init_14d_buckets(today: date) -> Dict[str, int]:
    """Create 14-day inclusive buckets keyed by ISO date string."""
    buckets: Dict[str, int] = {}
    for i in range(14):
        d = today - timedelta(days=i)
        buckets[d.isoformat()] = 0
    return buckets


@app.get(
    "/admin/analytics/repairs",
    tags=["Admin"],
    summary="Repairs analytics (admin)",
    description=(
        "Admin-only analytics for dashboards.\n"
        "Uses Supabase service role key to read all repairs (bypasses RLS). "
        "Therefore the API strictly enforces admin role."
    ),
    operation_id="admin_repairs_analytics",
    response_model=AdminRepairAnalyticsResponse,
)
async def admin_repairs_analytics(admin: UserContext = Depends(require_admin)) -> AdminRepairAnalyticsResponse:
    if _supabase is None:
        raise RuntimeError("Supabase client not initialized.")

    headers = postgrest_headers_service_role()

    rows = await _supabase.select(
        "repairs",
        headers=headers,
        select="id,status,created_at",
        order="created_at.desc",
    )
    if not isinstance(rows, list):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Unexpected response.")

    total = len(rows)
    by_status: Dict[str, int] = {}
    for r in rows:
        st = r.get("status") or "unknown"
        by_status[st] = by_status.get(st, 0) + 1

    today = datetime.utcnow().date()
    day_buckets = _init_14d_buckets(today)

    for r in rows:
        created_at = r.get("created_at")
        if not created_at:
            continue
        try:
            d = datetime.fromisoformat(created_at.replace("Z", "+00:00")).date()
        except Exception:
            continue
        key = d.isoformat()
        if key in day_buckets:
            day_buckets[key] += 1

    by_day_last_14d = [{"date": k, "count": day_buckets[k]} for k in sorted(day_buckets.keys())]

    return AdminRepairAnalyticsResponse(
        total_repairs=total,
        by_status=by_status,
        by_day_last_14d=by_day_last_14d,
    )


# Ensure OpenAPI is always reachable at /openapi.json (some deployments rely on it explicitly).
@app.get("/openapi.json", include_in_schema=False)
def openapi_json() -> Dict[str, Any]:
    """Return the generated OpenAPI schema."""
    return app.openapi()
