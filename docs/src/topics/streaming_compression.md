# Streaming compression

Django-Bolt compresses streaming responses (SSE, async generators, chunked
streams) the same way it compresses buffered responses: on-by-default,
governed by `CompressionConfig`, opt out per-route with `@no_compress`.
Each chunk is compressed with a per-chunk *sync flush* so the client
decoder surfaces it immediately — streaming semantics are preserved.

## Usage

```python
from django_bolt import BoltAPI
from django_bolt.responses import EventSourceResponse, StreamingResponse
from django_bolt.middleware import CompressionConfig, no_compress

# Defaults: brotli, gzip fallback, on for every response (buffered + streaming)
api = BoltAPI()


# Auto-compressed if the client advertises br (or gzip via fallback)
@api.get("/sse/feed")
async def feed():
    async def gen():
        for i in range(20):
            yield {"i": i, "msg": "x" * 200}

    return EventSourceResponse(gen())


# Per-route opt-out — same decorator as buffered responses
@api.get("/sse/raw")
@no_compress
async def raw():
    async def gen():
        yield {"plain": True}

    return EventSourceResponse(gen())


# Tune via CompressionConfig (same place as buffered tuning)
api = BoltAPI(compression=CompressionConfig(
    backend="brotli",        # or "gzip" / "zstd"
    minimum_size=500,        # buffered-only; ignored for streams
    gzip_fallback=True,
    brotli_quality=5,        # 0..=11
    brotli_lgwin=14,         # 10..=24 — 2^lgwin bytes window per conn (16 KiB default)
    gzip_level=6,            # 0..=9
    zstd_level=3,            # 1..=22
))

# Disable compression entirely
api = BoltAPI(compression=None)
```

The `StreamingResponse` / `EventSourceResponse` constructors don't take a
`compress=` kwarg — compression is decided per-request from the global
`CompressionConfig` plus the client's `Accept-Encoding`.

## Negotiation

For each streaming response Django-Bolt runs the same negotiation as the
buffered compression middleware:

1. If `@no_compress` is set on the route → `Content-Encoding: identity`.
2. If `BoltAPI(compression=None)` (or omitted explicitly) → identity.
3. If the client accepts `cfg.backend` → wrap with that codec.
4. Else if `gzip_fallback=True` and the client accepts `gzip` → wrap with
   the per-chunk gzip adapter.
5. Else → identity.

`Accept-Encoding: *` accepts any unmentioned coding; `*;q=0` rejects them.
An explicit `br;q=0` rejects brotli even when `*` is generous.

## Per-chunk flush

The wire-level flush is what makes streaming work — without it, the
encoder buffers events into its internal block, and the client doesn't
see anything until the block fills.

| Codec  | Flush mechanism                             | Decodable per chunk |
|--------|---------------------------------------------|---------------------|
| brotli | `CompressorWriter::flush()` → `BROTLI_OPERATION_FLUSH` | yes |
| gzip   | `GzEncoder::flush()` → `Z_SYNC_FLUSH`        | yes                 |
| zstd   | `zstd::stream::write::Encoder::flush()`     | yes                 |

One encoder runs per connection, so cross-chunk dictionary reuse still
benefits the compression ratio.

## Per-connection memory (brotli)

`lgwin` is the dominant memory knob. Window size = `2^lgwin` bytes per
connection. The default of `14` gives a 16 KiB window — enough for SSE
event vocabulary reuse, cheap enough for high-fanout servers.

| `lgwin`              | Window      | Marginal / conn (≈) | 1k conns | 10k conns |
|----------------------|-------------|---------------------|----------|-----------|
| 10 (min)             | 1 KiB       | ~10 KiB             | 10 MiB   | 100 MiB   |
| 14 (**default**)     | 16 KiB      | ~24 KiB             | 24 MiB   | 240 MiB   |
| 16                   | 64 KiB      | ~72 KiB             | 72 MiB   | 720 MiB   |
| 18                   | 256 KiB     | ~270 KiB            | 270 MiB  | 2.7 GiB   |
| 20                   | 1 MiB       | ~1 MiB              | 1 GiB    | 10 GiB    |
| 22 (brotli "normal") | 4 MiB       | ~4 MiB              | 4 GiB    | 40 GiB    |
| 24 (max)             | 16 MiB      | ~16 MiB             | 16 GiB   | 160 GiB   |

Drop `lgwin` for high-fanout SSE; raise it for large, repetitive bodies.

`quality` (0..=11) trades CPU for ratio. Per-event flush caps achievable
ratio anyway (each flush ends a self-contained block), so high quality
buys less on streaming than on one-shot compression. `5` is a sensible
default.

Gzip (`gzip_level` 0..=9) and zstd (`zstd_level` 1..=22) have similar
tradeoffs — defaults are tuned for the streaming case.

## Interaction with the global compression middleware

The buffered compression middleware reads `Content-Encoding` on the
outgoing response: if any value is pre-set, it passes the body through
unchanged. Streaming compression runs inside the handler and pre-sets
`Content-Encoding`, so the global middleware never re-wraps a stream —
no double-compression possible.

## Security — CRIME / BREACH

Compressing responses that mix attacker-influenced content with secrets
(session tokens, cookies, CSRF values, user identifiers) is vulnerable
to compression-ratio side-channel attacks. SSE is a particularly easy
target: per-event sizes are directly observable, and an attacker who
controls part of an event's content can probe for secret bytes one at
a time.

If your stream payloads can contain both attacker-controllable data
**and** secrets, either:

- Don't compress those responses (`@no_compress`), or
- Move the secret elsewhere (header, separate channel) so the
  compressed body never sees it.

This is the same risk class as HTTPS-level compression
([CRIME](https://en.wikipedia.org/wiki/CRIME) /
[BREACH](https://en.wikipedia.org/wiki/BREACH)); the framework
intentionally leaves the decision in your hands per-route rather than
disabling streaming compression globally.

## Implementation pointers

- `src/streaming_compression.rs` — `StreamCodec` enum, `EncoderStream`
  generic adapter, `select_stream_encoding`, zero-alloc Accept-Encoding
  parser.
- `src/streaming.rs::maybe_wrap_codec` — boxes the encoder around the
  Python chunk stream (and the keep-alive wrapper for SSE).
- `src/handler.rs::build_response_from_parsed` — reads
  `AppState.global_compression_config`, runs negotiation, sets the
  encoding headers + wraps the stream.
- `src/middleware/compression.rs` — bypasses any pre-set
  `Content-Encoding` so handler-owned encodings (streaming or otherwise)
  aren't re-wrapped.
