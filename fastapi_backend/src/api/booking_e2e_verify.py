"""
End-to-end verification for the public booking catalog flow.

This script verifies the minimum API sequence used by the React stepper:
  1) GET /api/brands
  2) GET /api/models?brand_id=<id>
  3) GET /api/issues

It is intentionally lightweight and does not require authentication.

Environment variables:
- BACKEND_BASE_URL (preferred) e.g. http://localhost:3001
- or API_BASE_URL (fallback)

Run:
  python -m src.api.booking_e2e_verify
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, List

import httpx


def _get_base_url() -> str:
    base = (os.getenv("BACKEND_BASE_URL") or os.getenv("API_BASE_URL") or "").rstrip("/")
    if not base:
        raise RuntimeError(
            "Missing BACKEND_BASE_URL (or API_BASE_URL). "
            "Set it to your running backend URL (e.g. http://localhost:3001)."
        )
    return base


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


# PUBLIC_INTERFACE
def run_booking_e2e_verify() -> None:
    """Run E2E verification for the public booking endpoints used by the frontend stepper."""
    base = _get_base_url()

    with httpx.Client(timeout=20) as client:
        r = client.get(f"{base}/api/brands")
        _assert(r.status_code == 200, f"GET /api/brands failed: {r.status_code} {r.text}")
        brands: List[Dict[str, Any]] = r.json()
        _assert(isinstance(brands, list), "Expected list from /api/brands")
        _assert(len(brands) > 0, "Expected at least 1 brand from /api/brands")

        # Prefer Samsung if present, else first.
        brand = next((b for b in brands if str(b.get("name", "")).lower() == "samsung"), brands[0])
        brand_id = brand.get("id")
        _assert(bool(brand_id), "Brand missing id field")

        r = client.get(f"{base}/api/models", params={"brand_id": brand_id})
        _assert(r.status_code == 200, f"GET /api/models failed: {r.status_code} {r.text}")
        models: List[Dict[str, Any]] = r.json()
        _assert(isinstance(models, list), "Expected list from /api/models")
        # Models may be empty if DB seed wasn't applied, but endpoint must succeed.
        if models:
            _assert(models[0].get("brand_id") == brand_id, "Model.brand_id does not match requested brand_id")

        r = client.get(f"{base}/api/issues")
        _assert(r.status_code == 200, f"GET /api/issues failed: {r.status_code} {r.text}")
        issues: List[Dict[str, Any]] = r.json()
        _assert(isinstance(issues, list), "Expected list from /api/issues")

    print("Booking E2E verify OK")


if __name__ == "__main__":
    try:
        run_booking_e2e_verify()
    except Exception as e:
        print(f"Booking E2E verify FAILED: {e}", file=sys.stderr)
        raise
