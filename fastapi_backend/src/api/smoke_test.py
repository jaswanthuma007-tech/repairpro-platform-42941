"""
Smoke test for the FastAPI backend.

This script is intentionally lightweight and can run in CI or locally to validate that:
- the backend is reachable
- the health endpoint responds
- the OpenAPI schema is served
- basic CORS preflight is configured (browser compatibility)
- required env wiring is present (Supabase + URL variables), when requested

Environment variables:
- BACKEND_BASE_URL (preferred) e.g. http://localhost:3001
- or API_BASE_URL (fallback)

Optional environment variables for additional validation:
- SMOKE_VALIDATE_ENV=1
    If set, the script will validate that SUPABASE_URL and SUPABASE_ANON_KEY exist
    (these are required for authenticated endpoints).
- SMOKE_ORIGIN
    Origin header to use for CORS preflight check (default: http://localhost:3000)

Run:
  python -m src.api.smoke_test
"""

import os
import sys
from typing import Any, Dict

import httpx


def _get_base_url() -> str:
    base = (os.getenv("BACKEND_BASE_URL") or os.getenv("API_BASE_URL") or "").rstrip("/")
    if not base:
        raise RuntimeError(
            "Missing BACKEND_BASE_URL (or API_BASE_URL). "
            "Set it to your running backend URL (e.g. http://localhost:3001)."
        )
    return base


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Missing required env var '{name}'. "
            "Ask the orchestrator to set it in the container .env."
        )
    return value


def _maybe_validate_env() -> None:
    """
    Validate minimal end-to-end env wiring if SMOKE_VALIDATE_ENV=1.
    This is a *smoke* validation only; it does not attempt Supabase auth flows.
    """
    if os.getenv("SMOKE_VALIDATE_ENV", "").strip() != "1":
        return

    # These are needed for authenticated endpoints and for PostgREST gateway headers.
    _require_env("SUPABASE_URL")
    _require_env("SUPABASE_ANON_KEY")
    # Service role key isn't required for all flows, so we don't enforce it here.


def _cors_preflight_check(client: httpx.Client, base_url: str, origin: str) -> None:
    """
    Validate that CORS middleware is active by making an OPTIONS preflight request.

    We intentionally check a "real" API route to ensure middleware is working.
    """
    headers = {
        "Origin": origin,
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "authorization,content-type",
    }
    r = client.options(f"{base_url}/openapi.json", headers=headers)

    # Starlette CORS middleware typically returns 200 or 204 for OPTIONS.
    if r.status_code not in (200, 204):
        raise AssertionError(f"CORS preflight failed: {r.status_code} {r.text}")

    # If CORS is configured, this header should be present for allowed origins.
    allow_origin = r.headers.get("access-control-allow-origin")
    if not allow_origin:
        raise AssertionError(
            "CORS preflight missing 'Access-Control-Allow-Origin'. "
            "Set CORS_ALLOW_ORIGINS to include the frontend origin."
        )

    # In many configs it will echo the origin; in wildcard configs it may be '*'.
    if allow_origin not in ("*", origin):
        raise AssertionError(
            f"Unexpected Access-Control-Allow-Origin value: '{allow_origin}' "
            f"(expected '*' or '{origin}')."
        )


# PUBLIC_INTERFACE
def run_smoke_test() -> None:
    """Run a small set of HTTP checks against the running backend."""
    base = _get_base_url()
    _maybe_validate_env()
    origin = (os.getenv("SMOKE_ORIGIN") or "http://localhost:3000").strip()

    def _assert(cond: bool, msg: str) -> None:
        if not cond:
            raise AssertionError(msg)

    with httpx.Client(timeout=10) as client:
        r = client.get(f"{base}/")
        _assert(r.status_code == 200, f"GET / failed: {r.status_code} {r.text}")
        data: Dict[str, Any] = r.json()
        _assert(data.get("message") == "Healthy", f"Unexpected health payload: {data}")

        r = client.get(f"{base}/openapi.json")
        _assert(r.status_code == 200, f"GET /openapi.json failed: {r.status_code} {r.text}")
        schema = r.json()
        _assert(schema.get("openapi"), "OpenAPI schema missing 'openapi' key")
        _assert(schema.get("paths"), "OpenAPI schema missing 'paths'")

        # CORS preflight validation (browser interoperability).
        _cors_preflight_check(client, base, origin)

    print("Smoke test OK")


if __name__ == "__main__":
    try:
        run_smoke_test()
    except Exception as e:
        print(f"Smoke test FAILED: {e}", file=sys.stderr)
        raise
