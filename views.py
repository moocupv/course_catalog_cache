import json
import logging
from datetime import datetime, timezone

import requests
from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

log = logging.getLogger(__name__)


# ---- Config cache/locks (ajustables) ----
CACHE_KEY = "upvx:courses_all:v1"
STALE_KEY = "upvx:courses_all:stale:v1"
LOCK_KEY = "upvx:courses_all:lock:v1"

CACHE_TTL_SECONDS = 15 * 60          # 15 min: lo “fresco”
STALE_TTL_SECONDS = 24 * 60 * 60     # 24 h: última buena (fallback)
LOCK_TTL_SECONDS = 20               # lock corto para refresco
REQUEST_TIMEOUT_SECONDS = 8         # no bloquear home


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _get_site_root():
    """
    Construye el root del LMS para poder llamarse a sí mismo.
    - Si hay LMS_ROOT_URL en settings, úsalo.
    - Si no, intenta inferirlo de request en runtime (ver _build_internal_url).
    """
    return getattr(settings, "LMS_ROOT_URL", "").rstrip("/")


def _build_internal_url(request, path):
    # Prioridad 1: setting explícito
    root = _get_site_root()
    if root:
        return f"{root}{path}"

    # Prioridad 2: inferir desde request
    scheme = "https" if request.is_secure() else "http"
    host = request.get_host()
    return f"{scheme}://{host}{path}"


def _fetch_all_courses_from_courses_api(request):
    """
    Llama al API nativo /api/courses/v1/courses/ paginando server-side.
    Nota: esto lo hace *una vez cada TTL* (o menos), no por usuario.
    """
    # Endpoint base (interno)
    url = _build_internal_url(request, "/api/courses/v1/courses/?page_size=100")

    # Importante: propagar cookies del usuario NO es necesario.
    # De hecho, conviene usar la llamada anónima si el endpoint es público.
    # Aun así, en algunos despliegues el endpoint puede requerir auth.
    # Para esos casos, puedes:
    #   - usar una credencial de servicio
    #   - o permitir cookies (requests) desde request.META (no recomendado)
    #
    # Aquí asumimos el caso típico: catálogo público.
    session = requests.Session()

    all_results = []
    page = 1

    while url:
        resp = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            raise RuntimeError(f"Courses API failed page={page} status={resp.status_code} body={resp.text[:300]}")

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
        "source": source,          # "cache" | "fresh" | "stale"
        "generated_at": _now_iso(),
        "version": 1,
    }
    return JsonResponse(payload, status=http_status, json_dumps_params={"ensure_ascii": False})


@require_GET
@never_cache  # la caché la hacemos nosotros (Redis). Evita caches intermedias raras.
def courses_all(request):
    """
    GET /api/upvx/courses/all

    Devuelve todos los cursos (sin paginación) como {results:[...]}.
    """
    # 1) Cache “fresh”
    cached = cache.get(CACHE_KEY)
    if cached is not None:
        return _json_response(cached, source="cache")

    # 2) No hay fresh: intenta lock para refrescar
    got_lock = cache.add(LOCK_KEY, "1", timeout=LOCK_TTL_SECONDS)

    if got_lock:
        try:
            results = _fetch_all_courses_from_courses_api(request)

            # Guardar fresh + stale
            cache.set(CACHE_KEY, results, timeout=CACHE_TTL_SECONDS)
            cache.set(STALE_KEY, results, timeout=STALE_TTL_SECONDS)

            return _json_response(results, source="fresh")

        except Exception as exc:
            log.exception("course_catalog_cache refresh failed: %s", exc)

            # Si falla el refresco, servir stale si existe
            stale = cache.get(STALE_KEY)
            if stale is not None:
                return _json_response(stale, source="stale", http_status=200)

            # Si no hay nada, devolver error (y el frontend mostrará su mensaje)
            return JsonResponse(
                {
                    "detail": "Could not refresh courses catalog and no stale cache available.",
                    "error": str(exc),
                    "generated_at": _now_iso(),
                },
                status=502,
            )
        finally:
            cache.delete(LOCK_KEY)

    # 3) No obtuvimos lock: otro worker está refrescando
    #    - intenta servir stale
    stale = cache.get(STALE_KEY)
    if stale is not None:
        return _json_response(stale, source="stale")

    # 4) Si tampoco hay stale, esperamos un poco y reintentamos leer fresh 1 vez
    # (sin dormir, para mantenerlo simple y no bloquear workers; si quieres, podemos meter un sleep pequeño)
    cached2 = cache.get(CACHE_KEY)
    if cached2 is not None:
        return _json_response(cached2, source="cache")

    return JsonResponse(
        {"detail": "Courses catalog is warming up. Please retry.", "generated_at": _now_iso()},
        status=503,
    )
