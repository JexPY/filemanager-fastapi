# Custodianship Prompt, Part 3 — Scale, Deliver, Grow

> Paste everything below the line into a fresh session with your most capable model.
> It picks up where Part 2 (`docs/RAISE_THE_CEILING_PROMPT.md`, the metadata pass) left off.
> Part 1 was `docs/ELITE_AUDIT_PROMPT.md` (the hardening pass).

---

You are a principal-level Python engineer continuing ownership of a media-processing
microservice. Two custodianship passes preceded you, both on branch `hardening/core-pass`
(based on `before_llm`):

1. **Hardening pass** (26 commits): audited every feature end to end, fixed every
   correctness/security bug, stood up a real test suite + CI, finished the api/ → app/
   migration, rewrote the docs.
2. **Metadata pass** (10 commits, the most recent history): added the system-of-record —
   a Postgres `uploads` table, per-token identity, owner-scoped `GET /files` /
   `GET /files/{id}` / `DELETE /files/{id}`, and idempotent image upload. Every piece was
   verified end to end against a clean stack, not just unit tests.

**Read `git log --oneline before_llm..HEAD` in full before doing anything else** — the
commits after `f5284d3` are the metadata pass. Do not re-derive what's already there;
build on it.

**This session is additive again**, one layer up: the service is now correct, covered,
and has a real system-of-record. Your job is to decide what makes it *production-scalable
and richer* — and build the highest-value pieces.

## Ground truth — read this first

- **`CLAUDE.md`** is the current, verified operational guide, updated through the metadata
  pass. Read it in full. Its "Non-obvious invariants" now include the metadata-store
  singleton, the video record state machine (`processing → ready/failed`, `mark_ready`
  swaps the raw key for the compressed key, mid-flight-delete discard), and image
  idempotency. Its "Known sharp edges & backlog" is your starting material — verify each
  item against the actual code before planning around it.
- **`readme.md`** has the current feature list, endpoint reference (incl. `/files` routes),
  and config reference.
- **Load-bearing details you must not casually break**: the per-process singleton pattern
  (storage *and* metadata), key-not-bytes-through-Redis (now three strings incl.
  `upload_id`), create-the-record-before-enqueue ordering, and the async-only
  request/worker paths. They're correct and deliberate.
- **Environment constraints are unchanged: no local Python venv, no host libvips/ffmpeg.**
  Everything runs in Docker. **The test service bakes source in at image-build time — run
  `docker compose run --rm --build test ...`, never without `--build`, or you silently
  re-run stale code** (this footgun bit the metadata pass twice; it's now in CLAUDE.md).
- **Commits: conventional-commit style, and do NOT add a `Co-Authored-By:` /
  AI-attribution trailer** (project convention, in CLAUDE.md).
- `hardening/core-pass` now carries *both* passes and is **still unmerged to `master`**.
  Confirm with the user where this session's work should land before assuming.

## Candidate work — argue for/against each, then choose

You picked the slate below with the repo owner; it is not a build-all list. Argue each in
this codebase's actual context, weigh sequencing/dependencies, and build the highest-value
subset. Read the real current code for anything you build on.

### Structural themes

- **Webhooks / push delivery.** Replace poll-only `GET /tasks/{id}` with a callback on
  video-compression completion. Now easy structurally: the `uploads` row already carries
  `owner` + `task_id` to fire against. But the hard parts are real — outbound HTTP from the
  **worker** (retries, backoff, dead-lettering), payload **signing (HMAC)** so receivers can
  verify authenticity, **SSRF risk** (the worker POSTing to a client-supplied URL — needs
  an allowlist / egress control), delivery semantics (at-least-once + an idempotency key),
  and *where the callback URL lives* (a column on `uploads`? per-token config?). Adjacent
  small fix: `GET /tasks/{id}` is **not** owner-scoped today — any valid token can poll any
  task_id; the record now makes scoping it straightforward.

- **Reliability & ops layer.** Retries/circuit breakers around each network hop (Redis,
  S3/GCS, imgproxy, Postgres), rate limiting, concurrency caps, structured observability.
  Argue honestly: the fail-closed pattern (`StorageError`/`MetadataError` → generic 502) is
  already correct and visible — this is a layer on top, not a fix. **Check what already
  exists** before adding: boto is configured with `retries={max_attempts:3}`, the asyncpg
  pool is bounded, logs already carry a request-id. Decide where rate limiting belongs
  (edge proxy vs. ASGI middleware vs. imgproxy's own limits) and whether there's even a
  metrics/tracing consumer to target yet.

- **Scale & large media.** Streaming I/O end-to-end + presigned direct-to-storage uploads.
  The biggest structural lever *and* the most invasive change. Streaming rewrites
  `StorageBackend.upload(bytes)` → a stream interface, every backend, the size-cap
  enforcement (you can no longer `len(data)` up front — count-as-you-stream and abort), the
  worker's download/compress/upload, and all fakes/tests. Presigned direct upload removes
  the API from the byte path but **changes the client contract to multi-step** and, more
  importantly, the API **loses its pre-store pyvips validate/strip step for images** — think
  hard about how validation, metadata recording, and imgproxy signing survive if bytes never
  touch the API. Weigh against the size caps (`MAX_*_UPLOAD_BYTES`) that already bound
  worst-case memory. If you do this, sequence it: streaming the video path (largest files)
  before images; presigned is a separate product decision.

- **Format breadth.** AVIF output, responsive multi-size derivatives, video
  thumbnails/posters, HLS. Lowest architectural risk — the pipeline is internally
  consistent. But argue demand: imgproxy already serves arbitrary on-the-fly sizes via
  signed URLs, so "responsive derivatives" may be a client concern, not server work. Video
  thumbnails/posters are a natural fit now (extract a frame → store it as its own image
  `uploads` record). HLS is a bigger change (many output objects + a playlist — a new video
  output model, not a drop-in).

### Small, self-contained items to fold in (owner-requested)

- **Alembic migrations.** The `uploads` schema is a single greenfield
  `CREATE TABLE IF NOT EXISTS` that the store self-creates on pool init; columns were added
  in place across the metadata pass because nothing was deployed. Introduce real, versioned
  migrations before any further schema change. **Reconcile the ownership shift**: migrations
  should own the schema, and the app should stop self-creating it (or keep a clearly
  demarcated bootstrap). Wire Alembic into the compose/CI startup path and verify a
  from-scratch `up` produces the same table.
- **`-t 60` truncation signal.** The ffmpeg command caps output at 60s (`tasks.py`),
  silently truncating longer inputs — the caller is never told (distinct from
  `FFMPEG_TIMEOUT_SECONDS`, the wall-clock kill). Fix properly: `ffprobe` the input
  duration and surface `truncated: true` (+ maybe duration) in the task result, and/or make
  the cap configurable. It's a response-shape change — in scope now. The `uploads` row could
  record duration/truncated too.
- **Video idempotency.** Images dedupe on `content_hash`; video doesn't (async,
  nondeterministic output). Dedupe on the **raw input** hash per owner using the existing
  `content_hash` column + `find_ready_by_hash` pattern — but decide whether to also short-
  circuit against a still-`processing` row (so two concurrent identical uploads don't both
  compress).

**Explicitly deferred** (owner did not pick it): GCS live verification — still no live GCP
credentials in this environment; don't fake it.

## How to work

Same discipline as both prior passes — it's what caught real bugs no unit test found:

1. **Verify, never assume.** Stand the stack up (`docker compose up --build`) and exercise
   each change end to end — before and after. A green unit suite is necessary, not
   sufficient; the metadata pass proved the worker→Postgres path and the imgproxy pipeline
   only by actually running them.
2. **Small, reversible, explained commits.** Conventional-commit style, each stating what
   changed, why, and how it was verified. No AI-attribution trailer.
3. **Write the test first** for new behavior. Reuse the fixtures: `fake_storage`,
   `fake_metadata` (seeds the in-memory `MetadataStore`), `fake_result_backend` /
   `fake_enqueue` for TaskIQ, and the `pg_integration` marker for real-Postgres tests.
4. **Update `CLAUDE.md` and `readme.md` in the same commit** as the change that needs them,
   so the docs never drift.
5. **Don't trust this list over the code.** It's a claim about state when written; read the
   actual current code for anything you build on.

## Definition of done for this session

- Whatever you build works end to end, demonstrated with command/output against the real
  stack — not just passing unit tests.
- Full suite still passes (it was 119 at the start of this session, incl. 5 `pg_integration`
  tests); CI still green; ruff + `ruff format --check` + `mypy app` clean.
- `CLAUDE.md` and `readme.md` reflect exactly what exists after your changes, every new
  command verified.
- A concise written report: what you built and why (tied to the candidates above or your
  own reasoning if you deviated), what you deliberately didn't build and why, and an updated
  backlog for whatever comes next.
