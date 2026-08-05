# Custodianship Prompt, Part 2 — Raise the Ceiling

> Paste everything below the line into a fresh session with your most capable model.
> It picks up exactly where the previous hardening pass (see `docs/ELITE_AUDIT_PROMPT.md`
> and `git log`) left off.

---

You are a principal-level Python engineer continuing ownership of a media-processing
microservice. A previous session ran a full custodianship pass on this codebase: it
audited every feature end to end, fixed every correctness/security bug it found, stood
up a real test suite (78 tests) and CI, finished the api/ → app/ migration, and rewrote
the docs. That work landed as 26 commits on branch `hardening/core-pass` (based on
`before_llm`, pushed to `origin`) — read `git log before_llm..hardening/core-pass` for
the full, itemized history before doing anything else. Do not re-derive what's already
there; build on it.

**This session's job is different in kind, not degree.** The previous pass was hardening:
fix what's broken, don't add scope. This one is additive: the codebase is now correct and
covered, and your job is to decide what raises its ceiling — and build the highest-value
pieces of it.

## Ground truth — read this first

- **`CLAUDE.md`** is the current, verified operational guide. Read it in full. Every
  command in it was actually run against the real stack before being written down. It
  ends with a "Known sharp edges & backlog" section — that section is your starting
  material, not a prescription. Verify each item is still accurate before planning
  around it; code moves faster than docs.
- **`readme.md`** has the user-facing feature list, endpoint reference, and config
  reference — also current as of the hardening pass.
- The architecture, invariants, and conventions in `CLAUDE.md` are load-bearing. Don't
  restructure the async model, the storage-singleton pattern, or the key-not-bytes-
  through-Redis design without a concrete reason tied to what you're building — they're
  correct and were deliberately preserved through the last pass.
- Same environment constraints as before: **no local Python venv, no host libvips/ffmpeg
  install.** Everything runs in Docker (`docker compose run --rm test ...`). If you're
  tempted to set up local tooling to move faster, don't — ask first if you think you have
  a real reason to deviate from this.
- The current branch (`hardening/core-pass`) has not been merged to `master`. Confirm
  with the user where this session's work should land — a new branch off
  `hardening/core-pass`, or something else — before assuming.

## Candidate work, from `CLAUDE.md`'s backlog

These are the ceiling-raising candidates already identified. Argue for or against each in
this codebase's actual context — don't build any of them just because they're listed.
Sequencing matters: several of these depend on each other.

- **A metadata/system-of-record** (even SQLite/Postgres) so uploads are listable,
  auditable, and deletable. Currently every object is fire-and-forget with no way to
  answer "what did token X upload" or "delete this specific object." This is likely the
  highest-leverage single addition: idempotency, a public delete endpoint, and
  per-token identity/audit trail all become straightforward once there's a record to
  attach them to, and stay awkward without one. Consider this first, not in isolation.
- **Idempotency / content-addressing** (hash-based keys) to dedupe re-uploads — depends
  on the metadata store above to do lookups.
- **A public `DELETE` endpoint** — `delete_file()` exists and is used internally
  (raw-video cleanup) but nothing exposes it externally. Needs the metadata store (or at
  minimum per-token identity) to scope safely — don't expose unscoped object deletion to
  any valid bearer token.
- **Per-token identity/scopes** — right now any valid token can call any route, including
  polling any task_id or (if you build the item above) deleting any object. No audit
  trail of who uploaded what.
- **Webhooks/callbacks** on video-compression completion, replacing poll-only
  `GET /tasks/{id}`.
- **Streaming I/O** end to end (`UploadFile` → S3 multipart; streamed downloads) so a
  1 GB video never sits fully in RAM. Flagged in `CLAUDE.md` as "the single biggest
  scalability lever" — but also the largest, most invasive change on this list. Weigh
  it against the size caps already in place (`MAX_IMAGE_UPLOAD_BYTES`/
  `MAX_VIDEO_UPLOAD_BYTES`), which bound worst-case memory without it.
- **Presigned direct-to-storage uploads** — removes the API from the upload path
  entirely for large media. Changes the client contract (multi-step upload flow instead
  of one POST); a bigger API-surface decision than a backend change.
- **Format breadth** — AVIF output, responsive/multi-size derivatives, video
  thumbnails/posters, HLS. Pure feature growth on a pipeline that's already internally
  consistent; lowest architectural risk, but check demand/priority before spending time
  here over the structural items above.
- **Retries/circuit breakers** around every network hop (Redis, S3/GCS, imgproxy). The
  existing fail-closed `StorageError → 502` pattern is correct and visible; this is a
  reliability layer on top, not a fix for broken behavior today.
- **Rate limiting / concurrency caps** — the size/timeout limits already close the worst
  unbounded-resource paths; decide if this is still worth a dedicated layer (ASGI
  middleware vs. edge proxy vs. imgproxy's own limits) given actual traffic patterns.
- **GCS live verification** — the GCS backend is unit-tested only (mocked client); no
  live GCP project/credentials were available in the previous session's environment. If
  you have access to a real GCP project, this is a small, valuable, low-risk task:
  round-trip a real bucket and confirm the existing code actually works, don't rewrite it
  speculatively.
- **Production reverse-proxy/TLS** — the old `nginx` service was removed in the hardening
  pass (it had no config and did nothing). Pick a reverse proxy / TLS terminator
  appropriate to the actual deployment target, if and when that target is known — this
  is arguably not this codebase's problem to solve in the abstract.
- **The `-t 60` ffmpeg output-duration cap silently truncates** anything longer, with no
  signal to the caller. Fixing this properly means either surfacing truncation in the
  task result or making the cap configurable — a response-shape change that was
  deliberately out of scope for the hardening pass. In scope now.

## How to work

Same discipline as the previous pass, because it's what caught three real bugs
(a startup crash, a worker boot failure, and a config typo) that no unit test found:

1. **Verify, never assume.** A feature isn't done because it looks right — stand the
   stack up (`docker compose up --build`) and actually exercise it end to end before and
   after your change, the same way the previous session caught the worker crash only by
   actually running `docker compose up` rather than trusting the test suite.
2. **Small, reversible, explained commits.** Conventional-commit style, matching the
   existing history — each says what changed, why, and how it was verified.
3. **Write the test first** for new behavior, the same pattern used throughout
   `hardening/core-pass`'s commit history (grep it for examples: fixture setup,
   `InMemoryStorageBackend`, `fake_result_backend`/`fake_enqueue` for TaskIQ).
4. **Update `CLAUDE.md` and `readme.md`** as you go, not at the end — a new invariant or
   env var introduced by this session's work should land in the same commit that
   introduces it, so the docs never drift out of sync the way they did before the last
   pass started.
5. **Don't assume the backlog list above is exhaustive or still accurate.** Read the
   actual current code for anything you're about to build on top of — a memory or a doc
   is a claim about the state of things when it was written, not a guarantee about now.

## Definition of done for this session

- Whatever you build works end to end, demonstrated with a command/output, against the
  real stack — not just passing unit tests.
- Test suite still passes in full; CI still green.
- `CLAUDE.md` and `readme.md` reflect exactly what exists after your changes, with every
  new command verified before being written down.
- A concise written report: what you built and why (tied to the specific backlog
  items/justifications above, or your own reasoning if you deviated from them), what you
  deliberately didn't build and why, and an updated backlog for whatever comes after this.
