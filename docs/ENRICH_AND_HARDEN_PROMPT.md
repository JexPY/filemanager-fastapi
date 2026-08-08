# Custodianship Prompt, Part 4 — Enrich the Outputs, Harden the Delivery

> Paste everything below the line into a fresh session with your most capable model.
> It picks up where Part 3 (`docs/SCALE_DELIVER_GROW_PROMPT.md`, the scale/deliver/grow
> pass) left off. Part 2 was `docs/RAISE_THE_CEILING_PROMPT.md` (metadata pass); Part 1 was
> `docs/ELITE_AUDIT_PROMPT.md` (hardening pass).

---

You are a principal-level Python engineer continuing ownership of a media-processing
microservice. Three custodianship passes preceded you, all on branch `hardening/core-pass`
(based on `before_llm`):

1. **Hardening pass** (26 commits): audited every feature end to end, fixed every
   correctness/security bug, stood up a real test suite + CI, finished the api/ → app/
   migration, rewrote the docs.
2. **Metadata pass** (10 commits): added the system-of-record — a Postgres `uploads` table,
   per-token identity, owner-scoped `GET /files` / `GET /files/{id}` / `DELETE /files/{id}`,
   and idempotent image upload.
3. **Scale/deliver/grow pass** (7 commits, the most recent history): **Alembic** now owns
   the `uploads` schema (a one-shot `migrate` compose service applies `upgrade head` before
   api/worker start; the store no longer self-creates it); video output-truncation is
   surfaced (`ffprobe` + `VIDEO_MAX_DURATION_SECONDS`, `truncated`/`duration_seconds` on the
   result and row); video upload is idempotent on the raw-input hash (`ready`→200,
   `processing`→202, `failed` re-processes); `GET /tasks/{id}` is owner-scoped via the record
   (the Redis issuance marker was retired); and **signed, SSRF-guarded webhooks** push
   `video.completed`/`video.failed` to a client `callback_url`. Every piece was verified end
   to end against a clean stack, not just unit tests.

**Read `git log --oneline before_llm..HEAD` in full before doing anything else** — the
commits after `c032194` are the scale/deliver/grow pass. Do not re-derive what's already
there; build on it.

**This session is additive again.** The service is correct, covered, has a real
system-of-record, versioned migrations, and push delivery. Your job is to make its outputs
*richer* and its newest delivery path *more robust* — and build the highest-value pieces.

## Ground truth — read this first

- **`CLAUDE.md`** is the current, verified operational guide, updated through the
  scale/deliver/grow pass. Read it in full. Its "Non-obvious invariants" now include the
  Alembic-owned schema + `migrate` service, the truncation signal, video idempotency
  (`find_active_video_by_hash`, `ready`/`processing`/`failed` semantics), owner-scoped
  `GET /tasks/{id}` via `get_by_task_id`, and the webhook contract (api-side SSRF admission
  vs. worker-side signed delivery). Its "Known sharp edges & backlog" is your starting
  material — verify each item against the actual code before planning around it.
- **`readme.md`** has the current feature list, endpoint reference (incl. `callback_url` on
  video upload), the **Webhooks** section (payload + signature contract), and config table.
- **Load-bearing details you must not casually break**: the per-process singleton pattern
  (storage *and* metadata), key-not-bytes-through-Redis (three strings incl. `upload_id`),
  create-the-record-before-enqueue ordering, the async-only request/worker paths, and now
  **Alembic owning the schema** (add a new revision under `migrations/versions/`; never edit
  the DDL in place, never re-add self-creation to the store) and the **webhook admission /
  delivery split** (SSRF checks live in the api at upload time; the worker only signs + POSTs
  a URL that was already admitted). They're correct and deliberate.
- **Environment constraints are unchanged: no local Python venv, no host libvips/ffmpeg.**
  Everything runs in Docker. **The test service bakes source in at image-build time — run
  `docker compose run --rm --build test ...`, never without `--build`, or you silently
  re-run stale code** (this footgun is in CLAUDE.md; it recurs every pass). If you add a
  Python dependency, regenerate `uv.lock` in a container (e.g.
  `docker run --rm -v "$PWD":/app -w /app ghcr.io/astral-sh/uv:python3.14-bookworm-slim uv lock`)
  — there is no host venv.
- **Schema changes go through Alembic now.** Add a revision, wire `down_revision` to the
  current head, verify a from-scratch `upgrade head` **and** a `downgrade`/re-`upgrade`
  round-trip against the real `db`. The `migrate` service (and the `test` service's
  dependency on it) already runs migrations before anything else.
- **Commits: conventional-commit style, and do NOT add a `Co-Authored-By:` /
  AI-attribution trailer** (project convention, in CLAUDE.md).
- `hardening/core-pass` now carries *all three* prior passes and is **still unmerged to
  `master`**. Confirm with the user where this session's work should land before assuming.

## Candidate work — argue for/against each, then choose

This is not a build-all list. Argue each in this codebase's actual context, weigh
sequencing/dependencies, and build the highest-value subset. Read the real current code for
anything you build on.

### Likely highest-value this session

- **Video thumbnails / posters.** The tastiest format item, and a natural fit now: in the
  worker, extract a frame (`ffmpeg -ss`) from the compressed (or raw) video, run it through
  the existing pyvips validate/strip → WebP path, and store it as **its own `uploads` image
  record** linked to the video (a `poster_upload_id` / `parent_id` column, decided via a new
  Alembic revision). Decide: extract always or on request? which timestamp (fixed vs.
  proportional)? does the completion webhook payload include the poster? Reuses the image
  pipeline end to end and is fully verifiable in this environment. Beware: the worker already
  imports pyvips transitively (that's why `Dockerfile.worker` installs libvips) — now it
  would call it directly, so make that dependency explicit.

- **Webhook robustness (harden what Part 3 shipped).** Two honest gaps the last pass left,
  both now in CLAUDE.md's backlog: (1) delivery is **awaited inline in the worker task**, so
  a slow/dead receiver ties up a worker slot for the whole retry budget — move delivery to
  its **own TaskIQ task** (the compression task enqueues it) so compression isn't blocked;
  (2) an exhausted delivery is **only logged**, not persisted — add a **dead-letter record**
  (a table, or a `webhook_*` state on the row) plus a **manual redelivery endpoint**
  (`POST /files/{id}/redeliver`, owner-scoped). Keep the existing HMAC signing, the stable
  `X-Webhook-Id` idempotency key, and the api-side SSRF admission intact. Verifiable with the
  same in-network-receiver technique the last pass used (a throwaway compose receiver + a
  compose override; see the session report / git history).

- **AVIF output (images).** Low architectural risk: add AVIF as an encode target alongside
  WebP in `image_vips.py`. Argue demand honestly first — imgproxy already serves AVIF
  on-the-fly from a signed URL if the *source* is stored, so the server-side win is mainly a
  smaller stored original / a canonical AVIF derivative. Decide whether it's worth a stored
  second object or is purely an imgproxy concern (it may be the latter).

### Bigger, demand-driven — probably NOT this session unless the owner says so

- **Streaming I/O + presigned direct-to-storage uploads.** The biggest structural lever and
  the most invasive change — it deserves its own dedicated pass, not a slice folded in with
  the above. Streaming rewrites `StorageBackend.upload(bytes)` → a stream interface, every
  backend, the size-cap enforcement (no more `len(data)` up front — count-as-you-stream and
  abort), the worker's download/compress/upload, and all fakes/tests. Presigned direct upload
  removes the API from the byte path but **changes the client contract to multi-step** and
  the API **loses its pre-store pyvips validate/strip for images** — think hard about how
  validation, metadata recording, and imgproxy signing survive if bytes never touch the API.
  The existing `MAX_*_UPLOAD_BYTES` caps already bound worst-case memory, so this is
  justified by *real* demand (much larger files / higher concurrency), not by default. If the
  owner greenlights it: sequence the **video path first** (largest files); presigned is a
  separate product decision after streaming lands.

### Explicitly parked (don't build without a concrete trigger)

- **Reliability & ops layer** (circuit breakers, rate limiting, tracing/metrics). Unchanged
  read from the last two passes: fail-closed is already correct and visible, boto has
  `retries={max_attempts:3}`, the asyncpg pool is bounded, logs carry a request-id, and there
  is **no metrics/tracing consumer to target**; rate limiting belongs at an edge proxy that
  isn't shipped. This is infrastructure with no consumer until one of those facts changes.
- **HLS / adaptive streaming.** A whole new video output model (many output objects + a
  playlist), not a drop-in. Only if adaptive streaming is an actual requirement.
- **GCS live verification.** Still no live GCP credentials in this environment; don't fake it.

## How to work

Same discipline as all three prior passes — it's what caught real bugs no unit test found:

1. **Verify, never assume.** Stand the stack up (`docker compose up --build`) and exercise
   each change end to end — before and after. A green unit suite is necessary, not
   sufficient; the last three passes proved the worker→Postgres, imgproxy, and
   worker→webhook-receiver paths only by actually running them (for webhooks: a throwaway
   compose `receiver` service + a `-f` override, so the worker POSTs to an in-network host you
   can read the logs of).
2. **Small, reversible, explained commits.** Conventional-commit style, each stating what
   changed, why, and how it was verified. No AI-attribution trailer.
3. **Write the test first** for new behavior. Reuse the fixtures: `fake_storage`,
   `fake_metadata` (seeds the in-memory `MetadataStore`), `fake_result_backend` /
   `fake_enqueue` for TaskIQ, the `pg_integration` marker for real-Postgres tests, and the
   in-process `aiohttp` receiver pattern in `tests/test_webhooks.py` for delivery.
4. **Schema changes are Alembic revisions**, verified with a from-scratch `upgrade head` and a
   `downgrade`→re-`upgrade` round-trip against the real `db`. Update `CLAUDE.md` and
   `readme.md` **in the same commit** as the change that needs them, so the docs never drift.
5. **Don't trust this list over the code.** It's a claim about state when written; read the
   actual current code for anything you build on.

## Definition of done for this session

- Whatever you build works end to end, demonstrated with command/output against the real
  stack — not just passing unit tests.
- Full suite still passes (it was **141** at the end of the last session, incl. `pg_integration`
  tests); CI still green; ruff + `ruff format --check` + `mypy app` clean.
- Any schema change is an Alembic revision that applies from scratch **and** round-trips.
- `CLAUDE.md` and `readme.md` reflect exactly what exists after your changes, every new
  command verified.
- A concise written report: what you built and why (tied to the candidates above or your own
  reasoning if you deviated), what you deliberately didn't build and why, and an updated
  backlog for whatever comes next.
