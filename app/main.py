import json as _json
import logging
from collections.abc import Coroutine
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from taskiq_fastapi import populate_dependency_context

from app.broker import broker
from app.config import _derive_owner, settings
from app.middleware import RequestIDLogFilter, RequestIDMiddleware
from app.routers import (
    auth,
    batch,
    health,
    management,
    playback,
    posters,
    qr,
    tasks,
    upload,
    visibility,
    webhooks,
)
from app.services.metadata import close_metadata_store, get_metadata_store
from app.services.storage import close_storage


class _JSONFormatter(logging.Formatter):
    """Structured JSON log lines for log aggregators (Datadog, Loki, ELK)."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        if record.exc_info and record.exc_info[0] is not None:
            log_entry["exception"] = self.formatException(record.exc_info)
        return _json.dumps(log_entry)


_handler = logging.StreamHandler()
_handler.addFilter(RequestIDLogFilter())
_handler.setFormatter(_JSONFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_handler])

# Uvicorn configures its own handlers on these loggers via dictConfig, bypassing
# the root config above -- so its access/error lines would otherwise stay plain
# text and break a JSON log pipeline (one un-parseable line per request). Point
# them at the same JSON handler, with propagation off so they don't also
# double-emit through root. This runs after uvicorn's own logging setup (the app
# is imported after Config() has configured logging), so it wins.
for _uvicorn_logger in ("uvicorn", "uvicorn.access", "uvicorn.error"):
    _l = logging.getLogger(_uvicorn_logger)
    _l.handlers = [_handler]
    _l.propagate = False


logger = logging.getLogger(__name__)


async def _safe_shutdown(name: str, coro: Coroutine[object, object, object]) -> None:
    """Run one shutdown step in isolation: a failure closing one pooled
    client (e.g. an already-broken asyncpg pool) must not prevent the rest
    from running and leaking their own resources."""
    try:
        await coro
    except Exception:
        logger.exception("Failed to close %s cleanly during shutdown", name)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Not fatal (see Settings.public_images_unservable), but silent breakage is
    # worse than a loud line at boot: every public image URL would 403 at
    # imgproxy long after the upload reported success.
    if settings.public_images_unservable:
        logger.warning(
            "STORAGE_BACKEND=%s has no *_PUBLIC_BASE_URL: imgproxy cannot fetch image "
            "sources, so thumbnail_url on public records will not resolve. "
            "Set one unless this deployment stores private media only.",
            settings.STORAGE_BACKEND,
        )
    for raw in settings.FILE_MANAGER_BEARER_TOKENS.split(","):
        entry = raw.strip()
        if entry and ":" not in entry:
            derived = _derive_owner(entry)
            logger.warning(
                "Unlabelled bearer token in FILE_MANAGER_BEARER_TOKENS fell back to "
                "derived owner '%s'. Use 'label:secret' format to ensure explicit, "
                "verifiable tenant identities.",
                derived,
            )
    # Initialize TaskIQ context for dependencies
    if not broker.is_worker_process:
        await broker.startup()
    populate_dependency_context(broker, app)
    # Build the metadata pool at startup so a bad DATABASE_URL fails fast. The
    # `uploads` schema itself is owned by Alembic and applied by the `migrate`
    # step before this process starts -- not created here.
    store = await get_metadata_store()
    await store.connect()
    yield
    # Release pooled storage + metadata clients held by the web process. Each
    # step is isolated via _safe_shutdown (see above) -- a failure in one must
    # not skip the rest and leak whatever comes after it.
    await _safe_shutdown("metadata store", close_metadata_store())
    await _safe_shutdown("storage backend", close_storage())
    await _safe_shutdown("health-check redis", health.close_redis())
    if not broker.is_worker_process:
        await _safe_shutdown("broker", broker.shutdown())


# Tag order here is display order in the docs -- Swagger UI preserves the
# order tags are declared in (via openapi_tags), it does not sort them
# alphabetically unless told to. This is the one place the route taxonomy is
# defined; individual routes just reference a name below via `tags=[...]`.
tags_metadata = [
    {
        "name": "Uploads",
        "description": (
            "Ingest new media. Images are validated, stripped of metadata, and "
            "stored synchronously; video is staged and compressed asynchronously "
            "by a separate TaskIQ worker -- poll its `task_id` under **Tasks** "
            "until the record turns `ready`. Both kinds dedupe per-owner on the "
            "input's SHA-256 hash, so re-posting identical bytes is a no-op."
        ),
    },
    {
        "name": "Tasks",
        "description": (
            "Poll the status of an asynchronous video compression job. Owner-"
            "scoped: a task id that isn't yours (or never existed) 404s."
        ),
    },
    {
        "name": "Files",
        "description": (
            "The system of record: list, fetch, adjust visibility on, and delete "
            "your own uploads. Every route here is owner-scoped -- another "
            "owner's file 404s, it never 403s, so existence never leaks."
        ),
    },
    {
        "name": "Sharing & Playback",
        "description": (
            "Stream a video and, optionally, hand out access to it. "
            "`/files/{id}/download` is the permanent, backend-agnostic playback "
            "URL with working HTTP Range on every storage backend; a share link "
            "is a separate, unlisted, revocable capability that bypasses "
            "visibility and auth entirely -- treat the token as a secret."
        ),
    },
    {
        "name": "Posters",
        "description": (
            "On-request thumbnail generation for a *ready* video. The result is "
            "an ordinary image record, linked back to its parent video."
        ),
    },
    {
        "name": "Webhooks",
        "description": (
            "Manual replay for a dead-lettered `callback_url` delivery. Only "
            "relevant when a video was uploaded with a callback and its "
            "delivery is exhausted (`webhook_status=failed`)."
        ),
    },
    {
        "name": "QR Codes",
        "description": (
            "Stateless QR code generation. Returns a PNG directly -- nothing is "
            "stored, so there is no follow-up record to fetch."
        ),
    },
    {
        "name": "System",
        "description": "Liveness and readiness probes for orchestrators. No auth required.",
    },
]

app = FastAPI(
    title="Filemanager-Fastapi",
    summary="Distributed image, video, and QR microservice",
    description="""\
Synchronous image/QR handling in this process, asynchronous video compression
in a separate TaskIQ worker process, behind one bearer-token identity per
caller.

* **Images** are validated, stripped of all metadata (EXIF/GPS/ICC/XMP), and
  served through imgproxy.
* **Video** is compressed off the request path; playback works with HTTP
  Range on every storage backend (local via nginx, S3, GCS).
* **Auth** is a single `Authorization: Bearer <token>` header -- see
  `FILE_MANAGER_BEARER_TOKENS`. Every route below is owner-scoped unless its
  description says otherwise.

Not a general file host: no cross-owner listing, no public search, and
deletion is explicit and irreversible.
""",
    version="2.0.0",
    lifespan=lifespan,
    openapi_tags=tags_metadata,
    swagger_ui_parameters={
        # Keep the Authorize token across a page refresh -- the single most
        # annoying default to *not* have when a route requires a bearer.
        "persistAuthorization": True,
        # Every tag's operations listed but collapsed; the docs read as a
        # table of contents first, full schemas on demand.
        "docExpansion": "list",
        # Hide the bottom "Schemas" wall by default -- it's noise until you
        # specifically need a model shape.
        "defaultModelsExpandDepth": -1,
        "displayRequestDuration": True,
        "filter": True,
        "tryItOutEnabled": True,
    },
)


def configure_cors(target: FastAPI) -> bool:
    """Mount CORS on `target` if any origins are configured; report whether it was.

    Call this *after* every other `add_middleware`: Starlette builds the stack so
    the most recently added middleware wraps everything before it, and CORS has to
    be outermost. That way a 4xx/5xx raised anywhere inside (an auth 401, a
    validation 422) still comes back with the headers, so the browser surfaces the
    real status instead of an opaque network error.

    With no origins configured nothing is mounted at all, so a pure
    backend-to-backend deployment keeps byte-identical responses.

    `allow_credentials=False` is deliberate, not an oversight: every credential
    this service accepts rides an `Authorization` header or a `?token=` query
    param, never a cookie. There is nothing for the browser to attach
    automatically, and therefore no cookie-CSRF surface to defend.

    Kept as a function rather than inline module code so it is callable against a
    throwaway app in tests -- the module-level `app` is built once at import, long
    before any test can monkeypatch `settings`.
    """
    origins = settings.parsed_cors_origins
    if not origins:
        return False
    target.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        max_age=600,
    )
    return True


app.add_middleware(RequestIDMiddleware)
configure_cors(app)

app.include_router(auth.router)
app.include_router(upload.router)
app.include_router(tasks.router)
# batch MUST come before management: /files/batch is a literal path that
# management's /files/{file_id} would otherwise shadow (Starlette matches
# routes in registration order, not by static-vs-dynamic specificity).
app.include_router(batch.router)
app.include_router(management.router)
app.include_router(visibility.router)
app.include_router(playback.router)
app.include_router(posters.router)
app.include_router(webhooks.router)
app.include_router(qr.router)
app.include_router(health.router)
