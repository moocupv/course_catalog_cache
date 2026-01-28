import logging
from datetime import datetime, timezone

import requests
from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

log = logging.getLogger(__name__)

# ---- Defaults (overridable via Django settings) ----
DEFAULTS = {
    "CACHE_KEY": "upvx:courses_all:v1",
    "STALE_KEY": "upvx:courses_all:stale:v1",
    "LOCK_KEY": "upvx:courses_all:lock:v1",
    "CACHE_TTL_SECONDS": 15 * 60,          # fresh TTL
    "STALE_TTL_SECONDS": 24 * 60 * 60,     # stale TTL
    "LOCK_TTL_SECONDS": 20,                # lock TTL
    "REQUEST_TIMEOUT_SECONDS": 8,          # upstream API timeout
    "PAGE_SIZE": 100,
    "API_PATH": "/api/courses/v1/courses/",  # public in your case
}


def _cfg(name: str):
    """
    Read from settings with prefix COURSE_CATALOG_CACHE_*.
    Example: COURSE_CATALOG_CACHE_CACHE_TTL_SECONDS = 900
    """
    return getattr(settings, f"COURSE_CATALOG_CACHE_{name}", DEFAULTS[name])


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _get_site_root():
    return getattr(settings, "LMS_ROOT_URL", "").rstrip("/")


def _build_internal_url(request, path, query: str = ""):
    root = _get_site_root()
    if root:
        return f"{root}{path}{query}"

    scheme = "https" if request.is_secure() else "http"
    host = request.get_host()
    return f"{scheme}://{host}{path}{query}"


def _fetch_all_courses_from_courses_api(request):
    """
    Calls the LMS Courses API and paginates server-side.
    This runs only on cache refresh (not per end-user request).
    Assumes the endpoint is public (no auth required).
    """
    page_size = int(_cfg("PAGE_SIZE"))
    api_path = _cfg("API_PATH")

    url = _build_internal_url(request, api_path, query=f"?page_size={page_size}")

    session = requests.Session()
    all_results = []
    page = 1

    while url:
        resp = session.get(url, timeout=float(_cfg("REQUEST_TIMEOUT_SECONDS")))
        if resp.status_code != 200:
            raise RuntimeError(
                f"Courses API failed page={page} status={resp.status_code} body={resp.text[:300]}"
            )

        data = resp.json()
        results = data.get("results") or []
        if not isinstance(results, list):
            results = []

        all_results.extend(results)

        pagination = data.get("pagination") or {}
        url = pagination.get("next")
        page += 1

    return all_results


def _json_response(results, source, http_status=200):
    payload = {
        "count": len(results),
        "results": results,
        "source": source,  # "cache" | "fresh" | "stale"
        "generated_at": _now_iso(),
        "version": 1,
    }
    return JsonResponse(payload, status=http_status, json_dumps_params={"ensure_ascii": False})


@require_GET
@never_cache  # we handle caching via Redis/Django cache; avoid intermediate caches
def courses_all(request):
    """
    GET /api/upvx/courses/all

    Returns all courses (no pagination) as {results:[...]}.
    """
    cache_key = _cfg("CACHE_KEY")
    stale_key = _cfg("STALE_KEY")
    lock_key = _cfg("LOCK_KEY")

    # 1) Fresh cache
    cached = cache.get(cache_key)
    if cached is not None:
        return _json_response(cached, source="cache")

    # 2) Refresh with distributed lock
    got_lock = cache.add(lock_key, "1", timeout=int(_cfg("LOCK_TTL_SECONDS")))

    if got_lock:
        try:
            results = _fetch_all_courses_from_courses_api(request)

            cache.set(cache_key, results, timeout=int(_cfg("CACHE_TTL_SECONDS")))
            cache.set(stale_key, results, timeout=int(_cfg("STALE_TTL_SECONDS")))

            return _json_response(results, source="fresh")

        except Exception as exc:
            log.exception("course_catalog_cache refresh failed: %s", exc)

            stale = cache.get(stale_key)
            if stale is not None:
                return _json_response(stale, source="stale", http_status=200)

            return JsonResponse(
                {
                    "detail": "Could not refresh courses catalog and no stale cache available.",
                    "error": str(exc),
                    "generated_at": _now_iso(),
                },
                status=502,
            )
        finally:
            cache.delete(lock_key)

    # 3) Someone else is refreshing: serve stale if possible
    stale = cache.get(stale_key)
    if stale is not None:
        return _json_response(stale, source="stale")

    # 4) Warmup fallback (no stale yet)
    cached2 = cache.get(cache_key)
    if cached2 is not None:
        return _json_response(cached2, source="cache")

    return JsonResponse(
        {"detail": "Courses catalog is warming up. Please retry.", "generated_at": _now_iso()},
        status=503,
    )
