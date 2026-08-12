# Custodianship Prompt — The Streaming-I/O Pass

> Paste everything below the line into a fresh session with your most capable model.
> Unlike the earlier `*_PROMPT.md` docs this repo used to carry (hardening →
> metadata → scale/deliver/grow → enrich-and-harden, then an unnumbered
> visibility/playback pass — all summarized in `CLAUDE.md`, the files themselves
> were deleted once folded in), this one is scoped and prescriptive, not a menu
> to argue over. It targets four specific, already-diagnosed issues and is
> designed to be run **one milestone at a time, in separate sessions** — paste
> the "Ground truth" section plus exactly one milestone, not the whole thing.

---

You are a principal-level Python engineer continuing ownership of a media-processing
microservice. Several custodianship passes preceded you on branch `hardening/core-pass`
(based on `before_llm`) — hardening, metadata/system-of-record, Alembic migrations +
video idempotency + webhooks, video posters + visibility/share-token playback. Read
`git log --oneline before_llm..HEAD` and `CLAUDE.md` in full before doing anything else.
Do not re-derive what's already there; build on it.

## Read this first — the tree may not be clean

At the time this prompt was written, `git status` showed a large *uncommitted* diff on
`hardening/core-pass`: `app/routers/files.py` deleted and split into
`auth.py`/`management.py`/`playback.py`/`posters.py`/`qr.py`/`tasks.py`/`upload.py`/
`utils.py`/`webhooks.py` (the Anti-Bloat rule in `CLAUDE.md` in action), a new
`app/routers/auth.py` (JWT + static-token dual auth), `docker-compose.override.yml`,
`docs/PRODUCTION.md`, `tests/test_jwt_auth.py`, and the four old `*_PROMPT.md` docs
deleted. **Run `git status` and `git diff --stat` before touching anything.** If that
diff is still there, confirm with the user whether to commit it (as its own commit,
separate from this pass's work) before starting — don't let an unrelated refactor and a
new streaming change land tangled in the same commit.

## Where this pass came from

An architectural review of the codebase raised four "this will blow up at scale" claims.
They were checked against the actual code, not taken at face value. Three were real (with
corrections); one was backwards. Don't re-litigate the diagnosis — it's settled below —
just execute.

1. **API upload buffering** — confirmed. `_read_capped` (now `app/routers/utils.py:13-27`)
   reads in 1 MB chunks but accumulates the whole thing into a `bytearray` before
   returning. With `MAX_VIDEO_UPLOAD_BYTES` defaulting to 500 MB
   (`app/config.py:83`), N concurrent uploads cost N × (up to 500 MB) of API RAM. Already
   named in `CLAUDE.md`'s backlog as *"No streaming I/O... the single biggest scalability
   lever."*
2. **Worker download buffering** — confirmed, and worse than first reported: it affects
   **both** `compress_video_task` (`app/tasks.py:84-86`) and `generate_poster_task`
   (`app/tasks.py:279-281`), each of which does `download_file()` (full object into RAM)
   then rewrites it to `/tmp` before ffmpeg ever runs, then reads the ffmpeg output fully
   back into RAM before re-upload. For the `local` backend this is pure waste — the bytes
   are already on local disk (`LocalStorage.download`, `app/services/storage.py:107-113`)
   and get copied through memory for no reason.
3. **"Hard deletions orphan storage / leak billing"** — **wrong as originally stated.**
   `delete_upload` (`app/routers/management.py:184-232`) already deletes the storage
   object *first* and only drops the Postgres row on success (see the docstring at
   `management.py:189-193`) — specifically to avoid the failure mode the report described.
   Don't build a soft-delete-plus-cron-reaper system; that solves a bug this code doesn't
   have. There **is** a real, narrower bug in the same neighborhood: if the storage
   delete succeeds but the subsequent `store.delete()` fails, the client gets a 502 and
   retries — and `GCSStorage.delete` (`app/services/storage.py:270-275`) likely raises on
   a 404 from the now-already-deleted object (`gcloud-aio-storage` surfaces a non-2xx as
   `aiohttp.ClientResponseError`, a subclass of the `aiohttp.ClientError` it catches),
   unlike `LocalStorage.delete` (`unlink(missing_ok=True)`) or S3's `delete_object` (204
   even on a missing key). If true, that retry permanently fails instead of self-healing.
   **Verify this against a real bucket or a tight mock before "fixing" it** — this
   environment has no live GCP credentials (see `CLAUDE.md`'s GCS caveat).
4. **Threadpool exhaustion from `asyncio.to_thread(_sha256_hex, ...)`** — the mechanism is
   real (`app/routers/upload.py:48,161` share the same anyio threadpool as pyvips encode
   and QR rasterization), but the report overstated the blast radius: `GET /files` etc.
   are pure `async def` over asyncpg and don't touch that pool, so they wouldn't stall.
   This isn't a separate fix — once ingestion streams (item 1), hash it incrementally with
   a rolling `hashlib.sha256()` updated per chunk instead of a single `to_thread` call over
   the whole buffer, and the CPU spike disappears on its own.

## Ground truth — read this first

- **`CLAUDE.md`** is the current, verified operational guide. Its "Non-obvious invariants"
  on storage-key-not-bytes-through-Redis, the per-process storage/metadata singletons, and
  the video record state machine are load-bearing — don't restructure them without a
  concrete reason.
- **`readme.md`** has the user-facing feature list, endpoint reference, and config table.
  Keep both docs in sync with whatever you ship, in the same commit.
- **Anti-Bloat is a hard rule now** (`CLAUDE.md` → Architectural Rules): files over ~400
  lines get split. `app/services/storage.py` is already at 355 lines
  (`wc -l app/services/storage.py`) — adding a streaming variant per backend (local/S3/GCS)
  on top of the existing buffered ones will likely push it over. Plan to split it into a
  package (`app/services/storage/__init__.py` + `local.py`/`s3.py`/`gcp.py`) rather than
  letting one file grow past the limit.
- **Docker-only, no local venv/host libvips/ffmpeg.** Everything — running the app, tests,
  lint, types — runs in Docker, matching production. `docker compose run --rm --build test
  pytest -v` (note `--build`: the test image bakes source in at build time, it is **not**
  bind-mounted — running without `--build` silently tests your old code).
- **Commits: conventional-commit style, explain what/why/how-verified. No
  `Co-Authored-By:` / AI-attribution trailer** (repo convention).
- Confirm with the user where this work should land — a new branch off
  `hardening/core-pass`, or something else.

## The milestones — pick one per session, in this order

Each is independently shippable and independently verifiable. Do not bundle two of these
into one commit/session; that's exactly the "everything at once" pattern that makes a
streaming rewrite risky to review.

### Milestone A — Delete-path idempotency (start here: smallest, safest)

Make `DELETE /files/{id}` retry-safe on every backend. Confirm (or refute, if the mock
doesn't match real GCS behavior) that a retried delete after a storage-delete-succeeded /
row-delete-failed race doesn't permanently 502. If `GCSStorage.delete` isn't idempotent on
a missing object, catch the not-found case explicitly and treat it as success, matching
`LocalStorage`/`S3Storage`. Add a unit test that deletes an already-gone key twice against
each fake/real backend available to you. This should be a small, self-contained diff —
`app/services/storage.py` (GCS class only) + a test. No API contract change.

### Milestone B — Worker stops double-buffering video bytes

`compress_video_task` and `generate_poster_task` (`app/tasks.py`) both do
download-into-RAM → write-to-`/tmp` → run ffmpeg → read-output-into-RAM → upload. Fix
both (they're the same pattern, don't fix one and forget the other):

- **`local` backend**: skip `download_file`/rewrite entirely — resolve the already-local
  path (mirror `_assert_safe_media_key` / the `LOCAL_STORAGE_DIR` join already used
  elsewhere) and hand that path straight to ffmpeg as `-i`. No copy at all.
- **`s3`/`gcp` backends**: use the existing `presigned_get_url()` (already implemented for
  both, used today for playback) and pass the signed URL as ffmpeg's `-i` instead of
  downloading first. Confirm ffmpeg's build in `Dockerfile.worker` has the network
  protocols compiled in (it should, by default) before relying on this.
- Output side: consider whether ffmpeg can write straight to a path you then hand to a
  streaming *upload* (depends on Milestone C/D's interface) — if that's not ready yet,
  it's fine to still buffer the (much smaller, compressed) output into RAM for now; the
  input side is where the real waste is.
- Keep the existing cleanup semantics (temp file removal in `finally`, raw-object deletion
  after processing, mid-flight-delete discard behavior for both `mark_ready` and
  `set_poster`) intact — those are load-bearing per `CLAUDE.md`.
- Verify against the real stack: compress an actual video on each configured backend
  (`local`, and `s3` via the MinIO dev profile at minimum) and confirm output is correct
  and the worker's peak RSS doesn't track the input file size anymore.

### Milestone C — API ingestion streams to disk instead of buffering in RAM

The core fix. Recommended target shape — bounded-memory, not necessarily zero-disk-touch:

1. Replace `_read_capped`'s `bytearray` accumulation with a stream straight to a temp file
   on disk, one chunk at a time (`aiofiles`, already a dependency). Keep the existing
   per-chunk size-cap abort and `request.is_disconnected()` check — don't regress those.
2. Compute the content hash incrementally: a rolling `hashlib.sha256()`, `.update()`d per
   chunk as it's written, `.hexdigest()`ed once at the end. This replaces the
   `asyncio.to_thread(_sha256_hex, file_data)` calls in `app/routers/upload.py:48,161` —
   folds milestone-4-from-the-review in for free, no separate work needed.
3. Give `StorageBackend` a streaming upload path that takes a file path (or an async
   iterator) instead of `bytes`, implemented per backend: `LocalStorage` can just
   move/copy the temp file; `S3Storage` can use `upload_file` (streams from disk, not
   memory) instead of `upload_fileobj(io.BytesIO(data))`; `GCSStorage` needs the
   equivalent streaming call in `gcloud-aio-storage`. Keep the existing `bytes`-based
   `upload()` too (images are small and simple `bytes` in/out is fine there — weigh
   whether image upload even needs this change, given `MAX_IMAGE_UPLOAD_BYTES` is 25 MB
   and already bounded; the video path is where this actually matters).
4. Update `tests/fakes.py::InMemoryStorageBackend` to support whichever new interface you
   land on, and update every test/fixture that constructs upload payloads accordingly.
5. Verify end to end: upload a real multi-hundred-MB video against `local` and MinIO
   `s3`, confirm the API process's peak RSS stays roughly flat regardless of file size
   (not just "tests pass") — this is the whole point of the change, so prove it, don't
   just assert it.

### Milestone D — (optional/stretch) true zero-disk-touch streaming to S3/GCS

Only take this on if Milestone C's disk-buffered version isn't good enough for your actual
traffic. Chains the incoming ASGI body stream directly into an S3 multipart upload / GCS
resumable upload without ever touching local disk. Meaningfully harder (multipart part-size
minimums, abort-on-failure cleanup of in-progress multipart uploads, no simple
`upload_file` shortcut) and lower value than Milestone C once C already makes memory
O(chunk size) instead of O(file size) — most of the win is already banked by C. Don't start
here.

## Explicitly out of scope for this pass

- **Presigned direct-to-storage uploads** (client → S3 directly, API never sees the
  bytes) — a different, bigger product decision (loses the pre-store pyvips
  validate/strip step for images), already tracked separately in `CLAUDE.md`'s backlog.
  Don't conflate it with this pass.
- **A soft-delete + reaper cron** — not needed; see Milestone A's actual scope.
- **HLS/ABR, CDN signed cookies** — unrelated, already parked in `CLAUDE.md`.

## How to work

Same discipline as every prior pass — it's what caught real bugs no unit test found:

1. **Verify, never assume.** Stand the stack up (`docker compose up --build`) and exercise
   each change end to end, before and after, with a real file of meaningful size — not
   just a green unit suite.
2. **One milestone per session/branch/PR.** Small, reversible, explained commits.
3. **Write the test first.** Reuse `fake_storage`/`fake_metadata` fixtures; update
   `tests/fakes.py` when the `StorageBackend` interface changes rather than working around
   it.
4. **Update `CLAUDE.md` and `readme.md` in the same commit** as the change that needs
   them.
5. **Don't trust this doc over the code.** It's a snapshot of file/line locations as of
   when it was written — confirm they still match before relying on them.

## Definition of done (per milestone)

- The specific claim in that milestone is demonstrably fixed against the real stack
  (describe how you proved it — RSS numbers, a retried-delete test, a real compression
  run — not just "tests pass").
- Full suite still green; `ruff check .`, `ruff format --check .`, `mypy app` clean.
- `CLAUDE.md`/`readme.md` reflect exactly what exists after the change.
- No file crossed the ~400-line Anti-Bloat threshold without being split.
