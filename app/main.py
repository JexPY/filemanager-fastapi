import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from taskiq_fastapi import populate_dependency_context

from app.broker import broker
from app.middleware import RequestIDLogFilter, RequestIDMiddleware
from app.routers import (
    auth,
    health,
    management,
    playback,
    posters,
    qr,
    tasks,
    upload,
    webhooks,
)
from app.services.metadata import close_metadata_store, get_metadata_store
from app.services.storage import close_storage

_handler = logging.StreamHandler()
_handler.addFilter(RequestIDLogFilter())
_handler.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s")
)
logging.basicConfig(level=logging.INFO, handlers=[_handler])


@asynccontextmanager
async def lifespan(app: FastAPI):
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
    # Release pooled storage + metadata clients held by the web process.
    await close_metadata_store()
    await close_storage()
    if not broker.is_worker_process:
        await broker.shutdown()


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

app.add_middleware(RequestIDMiddleware)

app.include_router(auth.router)
app.include_router(upload.router)
app.include_router(tasks.router)
app.include_router(management.router)
app.include_router(playback.router)
app.include_router(posters.router)
app.include_router(webhooks.router)
app.include_router(qr.router)
app.include_router(health.router)
