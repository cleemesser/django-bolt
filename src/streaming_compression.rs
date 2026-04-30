//! Per-chunk brotli compression for streaming responses.
//!
//! `BrotliStream` wraps a chunk stream and compresses each chunk independently
//! with `BROTLI_OPERATION_FLUSH` so chunks still arrive at the client one at a
//! time (preserving streaming semantics — including SSE event boundaries).
//! The encoder runs once per connection.

use actix_web::web::Bytes;
use brotli::CompressorWriter;
use futures_util::Stream;
use log::debug;
use pyo3::pybacked::PyBackedStr;
use pyo3::prelude::*;
use std::io::Write;
use std::pin::Pin;
use std::task::{Context, Poll};

/// Brotli encoder settings for a per-chunk compressed streaming response.
#[derive(Debug, Clone, Copy)]
pub struct StreamBrotliConfig {
    pub quality: u32, // 0..=11
    pub lgwin: u32,   // 10..=24
}

/// Validate `quality` (0..=11) and `lgwin` (10..=24) and return a
/// [`StreamBrotliConfig`].
///
/// Returns `None` (with a debug log) when either value is out of range.
/// Python-side `StreamingResponse` already validates these, but this acts
/// as a defensive guard for direct/test callers.
pub fn make_stream_brotli_config(quality: u32, lgwin: u32) -> Option<StreamBrotliConfig> {
    if quality > 11 || !(10..=24).contains(&lgwin) {
        debug!(
            "ignoring StreamingResponse brotli config: out of range quality={} lgwin={}",
            quality, lgwin
        );
        return None;
    }
    Some(StreamBrotliConfig { quality, lgwin })
}

/// Extract the per-chunk brotli config from a Python `StreamingResponse`
/// object's attributes (`compress`, `brotli_quality`, `brotli_lgwin`).
///
/// Returns `None` when:
/// - `compress` is `None` / absent (the common case — no compression),
/// - `compress` names an unsupported scheme (logs at debug),
/// - `brotli_quality` / `brotli_lgwin` are missing or out of range.
pub fn extract_brotli_config_from_response(
    response: &Bound<'_, PyAny>,
) -> Option<StreamBrotliConfig> {
    let py = response.py();
    let compress_attr = response.getattr(pyo3::intern!(py, "compress")).ok()?;
    if compress_attr.is_none() {
        return None;
    }
    // Zero-alloc string view borrowed from the Python str object.
    let compress: PyBackedStr = compress_attr.extract().ok()?;
    if &*compress != "br" {
        debug!(
            "ignoring StreamingResponse.compress: unsupported scheme '{}'",
            &*compress
        );
        return None;
    }
    let quality: u32 = response
        .getattr(pyo3::intern!(py, "brotli_quality"))
        .ok()?
        .extract()
        .ok()?;
    let lgwin: u32 = response
        .getattr(pyo3::intern!(py, "brotli_lgwin"))
        .ok()?
        .extract()
        .ok()?;
    make_stream_brotli_config(quality, lgwin)
}

/// In-memory sink that the brotli `CompressorWriter` writes into.
/// We pull the buffered bytes out via `get_mut()` after each `flush()`.
pub struct SinkWriter {
    pub buf: Vec<u8>,
}

impl Write for SinkWriter {
    fn write(&mut self, b: &[u8]) -> std::io::Result<usize> {
        self.buf.extend_from_slice(b);
        Ok(b.len())
    }

    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

/// Adapter Stream that brotli-compresses each chunk yielded by `inner`
/// and flushes per chunk.
pub struct BrotliStream<S> {
    inner: S,
    encoder: Option<CompressorWriter<SinkWriter>>,
    /// Set to `true` once the inner stream returns `Poll::Ready(None)`.
    /// Prevents re-polling a fused-exhausted inner stream (which panics in
    /// `futures_util::stream::unfold` if polled after `None`).
    inner_done: bool,
}

impl<S> BrotliStream<S> {
    pub fn new(inner: S, cfg: StreamBrotliConfig) -> Self {
        // Buffer size for the encoder's internal output buffer. 4 KiB matches
        // the reference impl and is plenty for a single SSE event or chunk.
        let sink = SinkWriter {
            buf: Vec::with_capacity(4096),
        };
        let encoder = CompressorWriter::new(sink, 4096, cfg.quality, cfg.lgwin);
        Self {
            inner,
            encoder: Some(encoder),
            inner_done: false,
        }
    }
}

impl<S> Stream for BrotliStream<S>
where
    S: Stream<Item = Result<Bytes, std::io::Error>> + Send + Unpin,
{
    type Item = Result<Bytes, std::io::Error>;

    fn poll_next(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        // Guard: never re-poll the inner stream after it returned `None`.
        // `futures_util::stream::unfold` panics if polled after exhaustion.
        if self.inner_done {
            return Poll::Ready(None);
        }
        loop {
            match Pin::new(&mut self.inner).poll_next(cx) {
                Poll::Ready(Some(Ok(chunk))) => {
                    let enc = match self.encoder.as_mut() {
                        Some(e) => e,
                        None => return Poll::Ready(None),
                    };
                    if let Err(e) = enc.write_all(&chunk) {
                        return Poll::Ready(Some(Err(e)));
                    }
                    if let Err(e) = enc.flush() {
                        return Poll::Ready(Some(Err(e)));
                    }
                    let buf = std::mem::take(&mut enc.get_mut().buf);
                    if buf.is_empty() {
                        // Encoder didn't emit anything yet; pull another chunk.
                        continue;
                    }
                    return Poll::Ready(Some(Ok(Bytes::from(buf))));
                }
                Poll::Ready(Some(Err(e))) => return Poll::Ready(Some(Err(e))),
                Poll::Ready(None) => {
                    // Inner stream ended. Mark it so we never poll it again.
                    self.inner_done = true;
                    // Finalize the encoder; this emits BROTLI_OPERATION_FINISH
                    // and gives us any tail bytes.
                    if let Some(enc) = self.encoder.take() {
                        let sink = enc.into_inner();
                        let buf = sink.buf;
                        if !buf.is_empty() {
                            return Poll::Ready(Some(Ok(Bytes::from(buf))));
                        }
                    }
                    return Poll::Ready(None);
                }
                Poll::Pending => return Poll::Pending,
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use brotli::Decompressor;
    use futures_util::stream;
    use futures_util::StreamExt;
    use std::io::Read;

    fn cfg() -> StreamBrotliConfig {
        StreamBrotliConfig {
            quality: 5,
            lgwin: 18,
        }
    }

    fn decompress(bytes: &[u8]) -> Vec<u8> {
        let mut out = Vec::new();
        Decompressor::new(bytes, 4096)
            .read_to_end(&mut out)
            .expect("brotli decode");
        out
    }

    #[tokio::test]
    async fn roundtrip_decodes_to_concatenated_input() {
        let events: Vec<Result<Bytes, std::io::Error>> = vec![
            Ok(Bytes::from_static(b"data: one\n\n")),
            Ok(Bytes::from_static(b"data: two\n\n")),
            Ok(Bytes::from_static(b"data: three\n\n")),
        ];
        let inner = stream::iter(events);
        let mut wrapped = BrotliStream::new(inner, cfg());

        let mut compressed = Vec::new();
        while let Some(chunk) = wrapped.next().await {
            compressed.extend_from_slice(&chunk.expect("chunk"));
        }

        let decoded = decompress(&compressed);
        assert_eq!(
            decoded,
            b"data: one\n\ndata: two\n\ndata: three\n\n".to_vec()
        );
    }

    #[tokio::test]
    async fn emits_at_least_one_chunk_per_input_event() {
        // Three input events should produce at least three output chunks
        // (per-event flush). They may also produce a trailing finalize chunk.
        let events: Vec<Result<Bytes, std::io::Error>> = vec![
            Ok(Bytes::from_static(b"data: one\n\n")),
            Ok(Bytes::from_static(b"data: two\n\n")),
            Ok(Bytes::from_static(b"data: three\n\n")),
        ];
        let inner = stream::iter(events);
        let mut wrapped = BrotliStream::new(inner, cfg());

        let mut chunks: Vec<Bytes> = Vec::new();
        while let Some(chunk) = wrapped.next().await {
            chunks.push(chunk.expect("chunk"));
        }

        assert!(
            chunks.len() >= 3,
            "expected per-event flush: got {} chunks",
            chunks.len()
        );
    }

    #[tokio::test]
    async fn empty_inner_stream_yields_no_compressed_bytes_or_only_final() {
        let inner = stream::iter::<Vec<Result<Bytes, std::io::Error>>>(vec![]);
        let mut wrapped = BrotliStream::new(inner, cfg());

        let mut compressed = Vec::new();
        while let Some(chunk) = wrapped.next().await {
            compressed.extend_from_slice(&chunk.expect("chunk"));
        }

        // Either nothing at all, or only the brotli FINISH frame (which
        // decodes to empty).
        let decoded = decompress(&compressed);
        assert_eq!(decoded, Vec::<u8>::new());
    }

    #[test]
    fn make_config_accepts_valid_values() {
        let cfg = make_stream_brotli_config(7, 20).unwrap();
        assert_eq!(cfg.quality, 7);
        assert_eq!(cfg.lgwin, 20);
    }

    #[test]
    fn make_config_accepts_boundary_values() {
        assert!(make_stream_brotli_config(0, 10).is_some());
        assert!(make_stream_brotli_config(11, 24).is_some());
    }

    #[test]
    fn make_config_rejects_out_of_range() {
        assert!(make_stream_brotli_config(12, 18).is_none());
        assert!(make_stream_brotli_config(5, 9).is_none());
        assert!(make_stream_brotli_config(5, 25).is_none());
    }

    // `extract_brotli_config_from_response` is exercised end-to-end by the
    // Python integration tests under `python/tests/test_sse_brotli*.py` —
    // pyo3's `extension-module` feature prevents standalone Rust tests from
    // linking against the Python interpreter.
}
