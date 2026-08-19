# Concurrent rendition encoding — parked idea, not yet decided

Status: **not started, not committed to.** This is a write-up of an optional
follow-up identified during the 2026-08-19 upload-latency investigation (see
`CLAUDE.md`'s "Upload-latency investigation" section for the full measured
context that motivated it). The investigation's actual root-cause question is
answered and closed; this is a separate, optional micro-optimization on top of
already-healthy numbers. Read the "Is this worth doing?" section before
picking it up.

## Copy-pasteable prompt, if/when you decide to pursue this

> In filemanager-fastapi, `app/services/image_vips.py`'s
> `validate_and_strip_image` currently runs three libvips WebP encodes
> **sequentially** inside one `asyncio.to_thread` call from
> `app/routers/upload.py`: the main optimized image, then the `thumbnail`
> rendition, then the `medium` rendition (see `RENDITION_SPECS` in
> `app/services/renditions.py`). Measured on 2026-08-19 (see
> `docs/concurrent-encoding.md` for the full baseline table), these three
> steps take ~292ms + ~39ms + ~61ms = ~393ms sequentially on a 1920x1280
> photo at `optimization=balanced`, inside a container with 16 idle CPUs and
> `VIPS_CONCURRENCY` unset. Investigate running the three encodes
> concurrently (e.g. `asyncio.gather` over separate `asyncio.to_thread`
> calls) to see whether wall-clock drops toward ~max(292ms) instead of
> sum(393ms) — a theoretical ~25% reduction — or whether libvips' internal
> threadpool contention erases most of that gain in practice. Measure before
> and after with temporary, removable timing instrumentation (git-revert it
> when done, same as the prior investigation), verify byte-identical output
> vs. the sequential path, and run the full gate (ruff check, ruff format
> --check, mypy app, full pytest suite) before proposing to land it. Don't
> assume the gain is real until measured — the whole reason this is parked
> instead of already done is that it's unverified.

## Is this worth doing?

Probably not urgent. The original latency regression that motivated the
whole investigation is already resolved by the two effort-tuning fixes that
shipped earlier in the `hardening/reliability-pass` branch (rendition
`effort` 6→4, main-encode `effort` tied to `optimization`). Current
`balanced`-profile uploads complete in ~400ms end-to-end, of which storage
I/O and metadata writes are ~4ms combined — not a bottleneck. This idea only
shaves a further ~25% off a request that is no longer the problem it was.
Land it only if there's a concrete reason to care about the last ~100ms
(e.g. a stricter SLA, or bulk/concurrent-upload throughput where the CPU
headroom savings compound across requests).

## Measured baseline (2026-08-19)

Docker Desktop for Mac, 16 CPUs visible to the `api` container,
`STORAGE_BACKEND=local`, dev `docker-compose.override.yml` active (bind
mounts + `uvicorn --reload`), synthetic 1920x1280 JPEG (~1.1MB, gradient +
noise + alpha, flattened — same construction as the earlier effort-tuning
benchmark), uploaded through the real stack via nginx (`:9000` → `api`).

`optimization=balanced`, sequential (current code):

| phase | ms |
|---|---|
| read_input | 1.4 |
| sha256_hash | 1.0 |
| pyvips_decode | 0.8 |
| rendition_encode_thumbnail | 39.3 |
| rendition_encode_medium | 60.8 |
| main_encode | 291.9 |
| storage_write_main | 1.9 |
| storage_write_rendition_thumbnail | 1.0 |
| storage_write_rendition_medium | 0.9 |
| metadata_create | 1.2 |
| **TOTAL** | **404.2** |

`optimization=quality`: TOTAL 959.7ms (main_encode 851.1ms — effort=6, Q=95).
`optimization=size`: TOTAL 187.5ms (main_encode 91.9ms — also downscales to
max_dim=1280).

If concurrency worked perfectly with zero contention, `balanced` TOTAL would
drop toward roughly `404.2 - 393.2 + max(0.8, 39.3, 60.8, 291.9)` ≈ **~303ms**
(the three sequential encode phases collapsing to the slowest one). That's
the theoretical ceiling, not a promise — see the contention risk below.

## Implementation sketch

- `validate_and_strip_image` is a plain **synchronous** function by design
  (see `CLAUDE.md`'s Conventions: "CPU-bound work... is offloaded via
  `asyncio.to_thread` in the router layer, not inside the service functions
  themselves"). Two ways to add concurrency without breaking that rule:
  1. **Keep `image_vips.py` fully sync**, and move the concurrency decision
     to `app/routers/upload.py`: replace the single
     `asyncio.to_thread(validate_and_strip_image, ...)` call with
     orchestration across three separate sync helper functions (main encode,
     thumbnail encode, medium encode), each wrapped in its own
     `asyncio.to_thread`, combined via `asyncio.gather`. This fits the
     codebase's stated async/sync boundary convention.
  2. Make `validate_and_strip_image` itself `async def` and gather
     internally. **Don't do this** — it contradicts the documented
     convention and would need explicit owner sign-off to deviate from it.
- Decode + dimension/pixel-count guard (`sniff_format`,
  `pyvips.Image.new_from_buffer`, the `MAX_IMAGE_PIXELS` check) stays
  sequential and up front — it's cheap (0.8ms measured) and every encode
  branch depends on its result (the decoded `pyvips.Image` and the
  `optimization`-derived `max_dim`/`q_value`/`effort`).
- The three parallel branches all read from the same decoded source `image`
  object but each produces its own derived image
  (`image.thumbnail_image(...)` / `image.resize(...)`) and writes its own
  buffer (`.write_to_buffer(...)`) — no shared mutable state between them if
  pyvips images are truly immutable pipelines as documented. **Verify this
  isn't just assumed**: check pyvips/libvips docs on thread-safety of reusing
  one source `Image` across concurrent operations, and/or write a small
  concurrency stress test (run N concurrent encodes off one source image,
  assert every run produces byte-identical output).
- Reassemble the return shape `(optimized_buffer, content_type, width,
  height, renditions: dict[str, bytes])` from the gathered results — `width`/
  `height` must come specifically from the main-encode branch (post-resize),
  not from a rendition branch.
- Preserve the `generate_renditions=False` path (video poster generation
  reuses this function for a single frame → WebP encode with no renditions)
  — it should stay a single encode with no gather overhead, same as today.

## Verification before landing

1. Re-run the same temporary-instrumentation methodology as the original
   investigation (add `time.perf_counter` timers, log via a dedicated
   logger, measure through the real stack, then `git checkout` the
   instrumentation back out) against the concurrent version. Compare TOTAL
   and the encode-phase sum against this doc's baseline table, for
   `balanced`, `quality`, and `size`.
2. Confirm byte-identical output vs. the current sequential path for the
   same input (a test comparing output hashes old vs. new, or extending an
   existing upload/rendition test).
3. Confirm no correctness regression under real concurrent load — not just
   one request at a time, since the whole premise is shared CPU threadpool
   contention across simultaneous requests (bulk upload endpoint already
   allows 4 concurrent images; check whether concurrent encoding interacts
   badly with that existing concurrency limit).
4. Full gate: `ruff check`, `ruff format --check`, `mypy app`, full pytest
   suite (unit + `pg_integration` + `s3_integration`) — see `CLAUDE.md`
   commands section.
5. Respect the anti-bloat / cognitive-complexity rules in `CLAUDE.md` — if
   this pushes `image_vips.py` or `upload.py` over ~400 lines or a function
   over complexity 15, split accordingly.
