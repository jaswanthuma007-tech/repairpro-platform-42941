"""
Smoke test for the FastAPI backend.

This script is intentionally lightweight and can run in CI or locally to validate that:
- the backend is reachable
- the health endpoint responds
- the OpenAPI schema is served

Environment variables:
- BACKEND_BASE_URL (preferred) e.g. http://localhost:3001
- or API_BASE_URL (fallback)

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


# PUBLIC_INTERFACE
def run_smoke_test() -> None:
    """Run a small set of HTTP checks against the running backend."""
    base = _get_base_url()

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

    print("Smoke test OK")


if __name__ == "__main__":
    try:
        run_smoke_test()
    except Exception as e:
        print(f"Smoke test FAILED: {e}", file=sys.stderr)
        raise
