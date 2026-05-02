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
