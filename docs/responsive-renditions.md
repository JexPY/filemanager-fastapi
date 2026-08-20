# Responsive image widths — open decision, not yet made

Status: **undecided, nothing started.** This is a write-up of a design choice
surfaced on 2026-08-20 while assessing filemanager-fastapi's responsive
rendition design. It is not a bug and not a known-good plan — it is a fork in
the road with two defensible branches, written down so the choice is made
deliberately rather than by accident on the day someone needs a 960px-wide photo.

Read "Which branch?" before picking it up.

## The gap

`RENDITION_SPECS` in `app/services/renditions.py` currently holds **exactly one
entry**: `thumbnail` — 300x300, `crop=True`, i.e. center-**cropped** to a
square. The `medium` / `_m960` spec was deliberately removed earlier (see
`CLAUDE.md`'s "Materialized image renditions" section: "`medium_url` / `_m960`
was removed completely to simplify response shapes").

So a consuming app asking for "this photo at 960px wide, aspect ratio intact"
has, today, exactly two materialized options:

1. the full-size primary `.webp` (up to 1280px or 1920px on the long edge,
   depending on `optimization`), or
2. a 300x300 square crop.

Neither is a landscape 960px. For a thumbnail grid the square crop is right;
for an SEO/LCP-driven page rendering a hero image through something like
`next/image` with a responsive `srcset`, neither fits, and the page ends up
shipping the full-size original to a 400px-wide phone viewport.

Note this is **not** a missing capability — `custom_url` already serves
arbitrary dimensions via imgproxy (`app/routers/utils.py`, the
`custom_width`/`custom_height`/`custom_fit`/`custom_format` branch). The
question is which of the two existing mechanisms should carry responsive
widths, because that choice has real cost either way.

## The two branches

### A. On-demand via imgproxy (`custom_url`)

Run the `imgproxy` container and let clients request whatever width they need.

- **No extra storage**, no extra upload latency, any width on demand.
- The nginx origin shield (`proxy_cache_lock` + `proxy_cache_valid 200 24h` in
  `nginx/nginx.conf.template`) already exists for precisely this traffic shape,
  and collapses a concurrent stampede on one URL into a single encode.
- Costs one container, and puts a live encode on the serving path for a cold
  cache. Note the caveat already recorded in `CLAUDE.md`'s renditions section:
  `proxy_cache_lock` collapses concurrent requests for *the same* URL, but a
  catalog-wide CDN purge is thousands of *distinct* URLs, each its own cache
  key, each a genuine miss — and every miss is a real encode on the same box
  that may also be running ffmpeg.
- **Verified 2026-08-20:** with an object-store backend and a
  `*_PUBLIC_BASE_URL` set, imgproxy is otherwise entirely off the hot path —
  `url` and `thumbnail_url` resolve to direct CDN object URLs via
  `public_object_url()` (`app/services/metadata/types.py`,
  `app/services/renditions.py`). Choosing this branch is therefore a decision
  to *start* depending on imgproxy at serve time, not merely to keep using it.

### B. Materialize more renditions (e.g. `_w960`, `_w1440`)

Add entries to `RENDITION_SPECS` with `crop=False` so they fit within the box
and preserve aspect ratio.

- **Zero-hop CDN reads**, no imgproxy container, no serve-time encode, and the
  existing plumbing already handles it end to end: key derivation
  (`derive_rendition_key`), the `renditions` jsonb column, `DELETE` cascade,
  visibility rotation, and `/files/{id}/download?rendition=...`.
- Costs upload latency and storage on **every** image, forever, whether or not
  that width is ever fetched. Measured on 2026-08-19 (see
  `docs/concurrent-encoding.md` for the full baseline): a rendition encode is
  **~39ms** (300x300) to **~61ms** (960px) on a 1920x1280 photo at
  `optimization=balanced`, against a ~404ms total. Two more widths is roughly
  **+100ms on every image upload** — a ~25% regression on a request that a
  previous pass worked specifically to bring down.
- Adding a `crop=False` spec is the first use of that flag in production;
  `RenditionSpec.crop` exists and is honoured, but every current caller uses
  `crop=True`. Verify the `crop=False` path actually produces the expected
  fit-within-box result before relying on it.

## Which branch?

**Leaning A (imgproxy), for a photo-centric consumer product** — but this is a
lean, not a decision, and it is explicitly the owner's call.

The reasoning: responsive `srcset` wants *several* widths, and branch B's cost
is paid per-width, per-upload, unconditionally, in the synchronous request
path. Three widths would roughly double upload latency to buy CDN hops that
the origin shield largely eliminates anyway. Branch A's cost is paid per
distinct width actually requested, once, then cached.

Branch B becomes the better answer if any of these hold:
- the set of widths is small and fixed (one or two), and known up front;
- the deployment wants to run as few containers as possible (imgproxy is a
  whole extra service to operate, monitor, and keep pinned);
- serve-time latency variance is unacceptable and every read must be a flat
  object fetch;
- the box is CPU-constrained and also running ffmpeg — in which case moving
  encodes *off* the serving path and onto upload may be worth the latency.

A hybrid is legitimate and probably the real answer at scale: materialize the
one or two widths that dominate real traffic (the hero and the grid card),
serve the long tail through `custom_url`.

**Do not pick a branch from this document alone.** Decide it against a real
consumer's actual layout — the breakpoints their frontend uses, and whether
those widths are stable — not in the abstract.

## Implementation sketch

### If branch A

- Nothing to build in this service. `custom_url` already works; see
  `_image_response` in `app/routers/utils.py` and the guard just above the
  `signed_image_url` call, which deliberately suppresses a no-op
  `format=webp`-with-no-dimensions round trip.
- Deployment work only: run the `imgproxy` container, confirm
  `IMGPROXY_BASE_URL` points at the nginx-fronted path (`/imgproxy/`, never
  imgproxy directly — the cache lock is the whole point), and confirm
  `IMGPROXY_ALLOWED_SOURCES` covers the bucket/CDN prefix.
- Document the sanctioned widths for consumers so they don't invent arbitrary
  ones — every distinct width is a distinct cache key, and unbounded widths
  defeat the cache.

### If branch B

- Add specs to `RENDITION_SPECS` with `crop=False`. The rest is already
  generic: `derive_rendition_key`, `_derive_rendition_public_url`, the
  `renditions` jsonb column, and the `?rendition=` download path all key off
  the spec registry rather than hardcoding `thumbnail`.
- `_derive_rendition_public_url` currently resolves any materialized rendition
  to a direct CDN URL when a public base URL is set, and falls back to signed
  imgproxy otherwise — so pre-existing records (uploaded before the new spec
  existed, with no `_w960` object) transparently fall back to a live imgproxy
  transform. **That fallback means branch B does not remove the imgproxy
  dependency unless every historical record is backfilled.** Decide explicitly
  whether to backfill or accept the fallback.
- Keep `generate_renditions=False` working — video poster generation reuses
  `validate_and_strip_image` for a single frame → WebP encode and must not
  start encoding new widths (this exact waste was fixed once already; see
  `CLAUDE.md`'s reliability-fixes section).
- Renditions are still generated only when the caller passes `?thumbnail=true`
  — a per-request boolean, not per-rendition. Adding widths raises the question
  of whether that flag should become a list (`?renditions=thumb,w960`). Decide
  this before adding a second spec, not after.
- Note the interaction with `docs/concurrent-encoding.md`: more sequential
  encodes per upload strengthens the case for that parked change, since the
  encode phases sum rather than overlap.

## Verification before landing either branch

1. Measure. Re-run the temporary-instrumentation methodology from the
   2026-08-19 investigation (see `docs/concurrent-encoding.md`) and compare
   upload TOTAL against that baseline — branch B must be judged on its real
   added latency, not the ~40-61ms estimate quoted here.
2. For branch B, confirm `crop=False` output is genuinely fit-within-box with
   aspect ratio preserved, at both landscape and portrait inputs.
3. Confirm the full lifecycle still holds for any new rendition: `DELETE
   /files/{id}` cascades to the new objects, public→private visibility
   rotation rotates them, and `?rendition=<new>` serves them for a private
   record.
4. Full gate: `ruff check`, `ruff format --check`, `mypy app`, full pytest
   suite — see `CLAUDE.md`'s commands section.
5. Respect the anti-bloat and cognitive-complexity rules in `CLAUDE.md`.
