import hashlib
import json
import logging
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse, HttpResponseNotModified
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

log = logging.getLogger(__name__)

DEFAULTS = {
    "CACHE_KEY": "upvx:courses_all:v1",
    "STALE_KEY": "upvx:courses_all:stale:v1",
    "LOCK_KEY": "upvx:courses_all:lock:v1",
    "CACHE_TTL_SECONDS": 2 * 60 * 60,      # 2h fresh
    "STALE_TTL_SECONDS": 72 * 60 * 60,     # 72h stale grace
    "LOCK_TTL_SECONDS": 30,                # lock TTL
    "REQUEST_TIMEOUT_SECONDS": 10,
    "PAGE_SIZE": 200,
    "API_PATH": "/api/courses/v1/courses/",
    "HTTP_MAX_AGE_SECONDS": 0,             # sin CDN: 0 para no liar caches intermedias
}


def _cfg(name: str):
    return getattr(settings, f"COURSE_CATALOG_CACHE_{name}", DEFAULTS[name])


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _get_site_root():
    # OJO: en Open edX suele ser https://upvx.es (externo)
    return getattr(settings, "LMS_ROOT_URL", "").rstrip("/")


def _get_internal_api_root():
    # Recomendado en k8s: "http://lms:8000"
    return getattr(settings, "COURSE_CATALOG_CACHE_INTERNAL_API_ROOT", "").rstrip("/")


def _build_base_url(request) -> str:
    """
    Orden de preferencia:
    1) COURSE_CATALOG_CACHE_INTERNAL_API_ROOT (si existe) -> evita LB/WAF/redirects
    2) LMS_ROOT_URL (si existe)
    3) request scheme+host
    """
    internal = _get_internal_api_root()
    if internal:
        return internal

    root = _get_site_root()
    if root:
        return root

    scheme = "https" if request.is_secure() else "http"
    host = request.get_host()
    return f"{scheme}://{host}"


def _build_url(request, path: str, query: str = "") -> str:
    base = _build_base_url(request).rstrip("/") + "/"
    # urljoin maneja bien paths con/sin slash
    return urljoin(base, path.lstrip("/")) + query


def _fetch_all_courses_from_courses_api(request):
    page_size = int(_cfg("PAGE_SIZE"))
    api_path = _cfg("API_PATH")
    url = _build_url(request, api_path, query=f"?page_size={page_size}")

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


def _make_etag(payload_obj) -> str:
    b = json.dumps(
        payload_obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(b).hexdigest()


def _json_response(results, source, http_status=200, request=None):
    payload = {
        "count": len(results),
        "results": results,
        "source": source,  # cache|fresh|stale
        "generated_at": _now_iso(),
        "version": 1,
    }

    etag = _make_etag({"count": payload["count"], "results": payload["results"], "version": payload["version"]})

    # Soporte conditional GET (útil si hay proxies aunque max-age=0)
    if request is not None:
        inm = request.headers.get("If-None-Match")
        if inm and inm == etag:
            resp = HttpResponseNotModified()
            resp["ETag"] = etag
            resp["Vary"] = "Accept-Encoding"
            max_age = int(_cfg("HTTP_MAX_AGE_SECONDS"))
            resp["Cache-Control"] = f"public, max-age={max_age}"
            return resp

    resp = JsonResponse(payload, status=http_status, json_dumps_params={"ensure_ascii": False})
    max_age = int(_cfg("HTTP_MAX_AGE_SECONDS"))
    resp["Cache-Control"] = f"public, max-age={max_age}"
    resp["Vary"] = "Accept-Encoding"
    resp["ETag"] = etag
    return resp


def _internal_token_ok(request) -> bool:
    expected = getattr(settings, "COURSE_CATALOG_CACHE_INTERNAL_TOKEN", "")
    got = request.headers.get("X-Internal-Token", "")
    return bool(expected) and got == expected


@require_http_methods(["GET", "HEAD"])
def courses_all(request):
    cache_key = _cfg("CACHE_KEY")
    stale_key = _cfg("STALE_KEY")
    lock_key = _cfg("LOCK_KEY")

    # 1) Fresh cache
    cached = cache.get(cache_key)
    if cached is not None:
        return _json_response(cached, source="cache", request=request)

    # 2) Refresh with distributed lock
    got_lock = cache.add(lock_key, "1", timeout=int(_cfg("LOCK_TTL_SECONDS")))

    if got_lock:
        try:
            results = _fetch_all_courses_from_courses_api(request)
            cache.set(cache_key, results, timeout=int(_cfg("CACHE_TTL_SECONDS")))
            cache.set(stale_key, results, timeout=int(_cfg("STALE_TTL_SECONDS")))
            return _json_response(results, source="fresh", request=request)
        except Exception as exc:
            log.exception("course_catalog_cache refresh failed: %s", exc)

            stale = cache.get(stale_key)
            if stale is not None:
                return _json_response(stale, source="stale", http_status=200, request=request)

            return JsonResponse(
                {
                    "detail": "Could not refresh courses catalog and no stale cache available.",
                    "error": str(exc),
                    "generated_at": _now_iso(),
                },
                status=502,
                json_dumps_params={"ensure_ascii": False},
            )
        finally:
            cache.delete(lock_key)

    # 3) Someone else is refreshing: serve stale if possible
    stale = cache.get(stale_key)
    if stale is not None:
        return _json_response(stale, source="stale", request=request)

    return JsonResponse(
        {"detail": "Courses catalog is warming up. Please retry.", "generated_at": _now_iso()},
        status=503,
        json_dumps_params={"ensure_ascii": False},
    )


@csrf_exempt
@require_POST
def courses_refresh(request):
    if not _internal_token_ok(request):
        return JsonResponse({"ok": False, "detail": "Unauthorized"}, status=401, json_dumps_params={"ensure_ascii": False})

    cache_key = _cfg("CACHE_KEY")
    stale_key = _cfg("STALE_KEY")
    lock_key = _cfg("LOCK_KEY")

    got_lock = cache.add(lock_key, "1", timeout=int(_cfg("LOCK_TTL_SECONDS")))
    if not got_lock:
        stale = cache.get(stale_key) or cache.get(cache_key) or []
        return JsonResponse(
            {"ok": True, "detail": "Refresh already in progress", "count": len(stale), "refreshed_at": _now_iso()},
            status=202,
            json_dumps_params={"ensure_ascii": False},
        )

    try:
        results = _fetch_all_courses_from_courses_api(request)
        cache.set(cache_key, results, timeout=int(_cfg("CACHE_TTL_SECONDS")))
        cache.set(stale_key, results, timeout=int(_cfg("STALE_TTL_SECONDS")))
        return JsonResponse(
            {"ok": True, "count": len(results), "refreshed_at": _now_iso()},
            status=200,
            json_dumps_params={"ensure_ascii": False},
        )
    except Exception as exc:
        log.exception("courses_refresh failed: %s", exc)
        return JsonResponse(
            {"ok": False, "detail": "Refresh failed", "error": str(exc), "refreshed_at": _now_iso()},
            status=502,
            json_dumps_params={"ensure_ascii": False},
        )
    finally:
        cache.delete(lock_key)
