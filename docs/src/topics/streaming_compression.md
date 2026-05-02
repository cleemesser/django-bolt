# `streaming_compression.rs`

Per-chunk brotli compression for streaming responses (SSE, async generators,
chunked streams). The whole point of this module is to compress each
event/chunk independently while still letting it land at the client immediately
— so streaming semantics (per-event boundaries) are preserved.

A single brotli encoder runs per connection. Each chunk yielded by the inner
stream is fed into the encoder and the encoder is then **flushed**, producing a
complete syncable brotli block that the client can decode and surface right
away.

---

## Where the per-event flush happens

`BrotliStream::poll_next` (`src/streaming_compression.rs:129`).

After every successful inner-stream chunk:

```rust
// src/streaming_compression.rs:142-153
if let Err(e) = enc.write_all(&chunk) {
    return Poll::Ready(Some(Err(e)));
}
if let Err(e) = enc.flush() {                      // <-- the per-event flush
    return Poll::Ready(Some(Err(e)));
}
let buf = std::mem::take(&mut enc.get_mut().buf);
if buf.is_empty() {
    continue;
}
return Poll::Ready(Some(Ok(Bytes::from(buf))));
```

`CompressorWriter::flush()` issues `BROTLI_OPERATION_FLUSH` to the underlying
brotli encoder. That instructs brotli to emit a self-contained block ending on
a byte boundary, which is the property a client decoder needs to surface the
event immediately rather than waiting for more data. The `flush()` is invoked
once per chunk pulled from the inner stream — i.e. once per SSE event.

A second, terminal flush is performed when the inner stream ends — see
"`Poll::Ready(None)` branch" below.

---
## Usage - Set on the response at construction (Python):

```python
from django_bolt.responses import StreamingResponse # or EventSourceResponse

return StreamingResponse(
    generator(),
    compress="br",
    brotli_quality=5,
    brotli_lgwin=14,   # ← 16 KiB window per connection down from default of 18
)


## Marginal memory cost of adding the compressor

Cost is **on top of** an uncompressed streaming connection. If `compress=None`,
`maybe_wrap_brotli` is pass-through (`src/streaming.rs:241`) — marginal cost is
**0**.

| Source | Default (`q=5, lgwin=18`) | Notes |
|---|---|---|
| Brotli sliding window | **256 KiB** (`2^lgwin`) | dominant cost |
| Brotli hash tables (back-ref index) | **~tens of KiB** at q=5 | grows with quality; q=11 reaches hundreds of KiB |
| `CompressorWriter` internal output buffer | **4 KiB** | `CompressorWriter::new(sink, 4096, ...)` (`src/streaming_compression.rs:114`) |
| `SinkWriter.buf` preallocated capacity | **4 KiB** | `Vec::with_capacity(4096)` (`src/streaming_compression.rs:112`) |
| Outer `Box` from `Box::pin(BrotliStream::new(...))` | **~hundreds of bytes** inline (mostly `CompressorWriter` embedded in `Option`) | `src/streaming.rs:238` |

**Total marginal at defaults: ~270 KiB per connection**, almost entirely the
sliding window.

### What's *not* extra per connection

- The inner Python stream / keepalive wrapper / `Py<PyAny>` for the generator
  exist regardless of compression. `BrotliStream` moves the inner stream into
  itself — no duplication.
- The 4 KiB `SinkWriter.buf` is reused. Every `flush()` does `mem::take`,
  leaving an empty `Vec` with its capacity preserved for the next event.
  Steady-state, not per-event growth.
- No per-event allocation in steady state beyond the `Bytes` handed
  downstream.

---

## Tuning — `lgwin` (window log size)

`lgwin` is the dominant memory knob. Window size = `2^lgwin` bytes per
connection.

| `lgwin`              | Window      | Marginal/conn (≈) | 1k conns    | 10k conns   |
|----------------------|-------------|-------------------|-------------|-------------|
| 10 (min)             | 1 KiB       | ~10 KiB           | 10 MiB      | 100 MiB     |
| 14                   | 16 KiB      | ~24 KiB           | 24 MiB      | 240 MiB     |
| 16                   | 64 KiB      | ~72 KiB           | 72 MiB      | 720 MiB     |
| **18 (default)**     | **256 KiB** | **~270 KiB**      | **270 MiB** | **2.7 GiB** |
| 20                   | 1 MiB       | ~1 MiB            | 1 GiB       | 10 GiB      |
| 22 (brotli "normal") | 4 MiB       | ~4 MiB            | 4 GiB       | 40 GiB      |
| 24 (max)             | 16 MiB      | ~16 MiB           | 16 GiB      | 160 GiB     |

The brotli RFC notes that for short messages a smaller window costs almost
nothing in compression ratio. For SSE, where each event is typically a small
JSON payload, the window mostly helps with **cross-event vocabulary reuse**
(repeated keys, repeated string fragments). 256 KiB is plenty for that; values
above 18 are usually wasted dictionary you'll never fill. In future I may event
consider a lower default like 14 with the idea that you will know if you send
large payloads and can increase the value.

### When to drop `lgwin` below 18

- High fan-out SSE servers (thousands of concurrent clients) where 256 KiB ×
  N is uncomfortable.
- Small, self-contained events with little cross-event repetition.
- Try `lgwin=14` (16 KiB) or `lgwin=16` (64 KiB) — usually within ~1–3% of
  default ratio for SSE-shaped traffic, at a fraction of the memory.

### When to raise `lgwin`

- Large bodies (file streams, big JSON arrays) with lots of long-range
  repetition. Even then, 20 is usually enough; 22+ is for archival use cases,
  not live streaming.

---

## Tuning — `quality`

`quality` (0..=11) trades CPU for compression ratio. It also affects memory
via hash table sizes, but `lgwin` is far more impactful.

- **0–3**: very fast, modest ratio. Reasonable for tiny payloads where the
  fixed brotli overhead dominates.
- **4–6**: balanced. **Default `5`** is a sensible streaming choice.
- **7–9**: slower, better ratio. Worth it for cacheable bulk responses, not
  hot streaming paths.
- **10–11**: very slow encoder; hash tables can grow to hundreds of KiB; do
  not use on streaming endpoints under load.

Per-event flush (`BROTLI_OPERATION_FLUSH`) caps achievable ratio anyway —
each flush ends a self-contained block — so high quality buys less on
streaming than on one-shot compression.

---

## Quick capacity-planning rules

- Marginal cost ≈ **`2^lgwin` + small constant (~12 KiB)**.
- Per-connection budget = `2^lgwin` × concurrent compressed streams.
- `compress=None` connections cost nothing from this module.
- Multi-process: each worker process has its own per-connection memory; total
  = workers × concurrent-conns-per-worker × `2^lgwin`.



---
## Public surface

### `StreamBrotliConfig` (`:19`)

`Copy` config struct with two fields:

- `quality: u32` — 0..=11, brotli quality.
- `lgwin: u32` — 10..=24, brotli sliding window log size.

Held by value at `BrotliStream::new` time and by `streaming.rs` while wiring up
the response.

### `make_stream_brotli_config(quality, lgwin) -> Option<StreamBrotliConfig>` (`:31`)

Defensive validator. Returns `None` (with a `debug!` log) when either value is
out of range. Python-side `StreamingResponse` already validates, so this is
mainly a guard for direct/test callers.

### `extract_brotli_config_from_response(response: &Bound<PyAny>) -> Option<StreamBrotliConfig>` (`:49`)

Reads brotli config off a Python `StreamingResponse` object via PyO3:

- `compress` — `None` ⇒ `None` (no compression — the common case).
- `compress` ⇒ must be the literal string `"br"`; otherwise debug-log and
  return `None`.
- `brotli_quality` / `brotli_lgwin` — extracted as `u32` and run through
  `make_stream_brotli_config`.

`compress` is read as a `PyBackedStr` so the comparison is zero-alloc — it
borrows directly from the underlying Python `str`.

This is the bridge from Python config to Rust runtime; called from
`handler.rs:490` while building the streaming response.

### `SinkWriter` (`:81`)

A trivial in-memory `Write` impl wrapping a `Vec<u8>`. The brotli
`CompressorWriter` writes encoded bytes into this sink; `BrotliStream` then
moves those bytes out via `std::mem::take(&mut enc.get_mut().buf)` after each
flush. `flush()` on the sink is a no-op — the actual buffer drain happens at
the `BrotliStream` layer.

### `BrotliStream<S>` (`:98`)

The `Stream` adapter. Fields:

- `inner: S` — the upstream chunk stream.
- `encoder: Option<CompressorWriter<SinkWriter>>` — `Option` because we
  `take()` it during finalization to call `into_inner()` and recover the sink.
- `inner_done: bool` — sticky flag set the first time `inner` returns
  `Poll::Ready(None)`. Prevents re-polling a fused-exhausted stream;
  `futures_util::stream::unfold` panics if polled after `None`, which is the
  shape the upstream Python stream takes on the SSE path.

Constructor `BrotliStream::new` (`:108`) builds the `CompressorWriter` with a
4 KiB internal output buffer (matches the brotli reference impl, more than
enough for one SSE event).

---

## Trace: `Stream::poll_next` (`:129`)

1. **Re-poll guard** (`:132`). If `inner_done` is set, return
   `Poll::Ready(None)` immediately. Sticky guard against the unfold-panic
   described above.

2. **Loop on inner.poll_next** (`:135`). Looped because an encoder may consume
   a chunk without producing output bytes; in that case we transparently pull
   the next inner chunk rather than yielding an empty `Bytes`.

3. **`Poll::Ready(Some(Ok(chunk)))`** — the hot path:
   - `write_all(&chunk)` into the encoder.
   - `enc.flush()` — **this is the per-event flush**. Issues
     `BROTLI_OPERATION_FLUSH`, producing a complete brotli block in the sink.
   - `std::mem::take` the sink's `buf` out as a `Vec<u8>`, leaving an empty
     `Vec` behind for the next round.
   - Empty buffer ⇒ `continue` the loop (rare; happens if the encoder didn't
     emit on this flush).
   - Non-empty ⇒ `Poll::Ready(Some(Ok(Bytes::from(buf))))`.

4. **`Poll::Ready(Some(Err(e)))`** — propagate the upstream error verbatim.

5. **`Poll::Ready(None)`** — terminal/finalize branch:
   - Set `inner_done = true`.
   - `self.encoder.take()` and call `enc.into_inner()` to recover the
     `SinkWriter`. `into_inner()` performs the brotli `BROTLI_OPERATION_FINISH`
     finalization, writing any trailing bytes (final block + EOS marker) into
     the sink.
   - If the sink has bytes, yield one final `Poll::Ready(Some(Ok(...)))`.
   - Otherwise yield `Poll::Ready(None)` — stream ends.

6. **`Poll::Pending`** — propagate.

Note: error/finalize paths leave `encoder = None`. A subsequent poll falling
through to `:138` (`encoder.as_mut()`) returns `Poll::Ready(None)`, so the
stream is correctly fused even outside the `inner_done` path.

---

## Wiring

The compressor sits at the very edge of the response pipeline, after
keep-alive injection:

```text
Python async/sync generator
  └─ create_python_stream_with_config (streaming.rs)
     └─ keepalive_stream (optional)         ← injects ": ping\n\n"
        └─ BrotliStream (optional)          ← per-chunk compress + flush
           └─ Actix HttpResponse body
```

- `streaming.rs:214 create_sse_stream` builds the inner Python stream, wraps
  it with `keepalive_stream` if a ping interval is configured, then calls
  `maybe_wrap_brotli`.
- `streaming.rs:233 maybe_wrap_brotli` boxes a `BrotliStream::new(inner, cfg)`
  when a config is present; pass-through otherwise.
- `handler.rs:490` extracts the config from the Python response object via
  `extract_brotli_config_from_response`; that config is then threaded through
  `response_builder.rs` and into the streaming construction.

Because compression runs **after** keep-alive injection, ping frames are also
compressed and flushed — they remain decodable independently.

---

## Tests (`:177` onward)

- `roundtrip_decodes_to_concatenated_input` — feed three SSE events in,
  concatenate all compressed output, decompress; verifies content equality.
- `emits_at_least_one_chunk_per_input_event` — three input events ⇒ at least
  three output chunks. This is the test that pins the per-event-flush
  contract.
- `empty_inner_stream_yields_no_compressed_bytes_or_only_final` — empty input
  decodes to empty output (only the FINISH frame, or nothing at all).
- `make_config_*` — boundary and out-of-range validation.

`extract_brotli_config_from_response` is not unit-tested in Rust because the
crate's pyo3 `extension-module` feature blocks linking a standalone Rust test
binary against `libpython`; coverage lives in
`python/tests/test_sse_brotli*.py`.

---

## Why one encoder per connection (vs. per chunk)

Brotli's compression ratio comes from its sliding-window dictionary; tearing
down and rebuilding the encoder per chunk would discard that state and balloon
output size. Keeping a single encoder and flushing per chunk gives the best of
both: streaming semantics (each event ships immediately, decodable on its own)
**and** good compression ratio across events that share vocabulary (which is
the common case for JSON-shaped SSE payloads and even more so for hypertext
oriented payloads with html or svg elements).
