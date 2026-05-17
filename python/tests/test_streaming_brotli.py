"""End-to-end tests for default-on streaming compression.

The framework auto-compresses streaming responses based on the
``CompressionConfig`` attached to the ``BoltAPI`` (same model as buffered
responses). Per-route opt-out: ``@no_compress``.

httpx auto-decodes ``Content-Encoding: br`` / ``gzip`` / ``zstd`` when the
corresponding decoder package is installed, so ``resp.content`` is the
*decoded* plain bytes. Tests:
  1. Assert ``Content-Encoding`` to confirm wire compression was applied.
  2. Inspect ``resp.content`` to verify body integrity.
"""

from __future__ import annotations

import asyncio

import pytest

brotli = pytest.importorskip("brotli", reason="brotli or brotlicffi package not installed")

from django_bolt import BoltAPI
from django_bolt.middleware import CompressionConfig, no_compress
from django_bolt.responses import EventSourceResponse, StreamingResponse
from django_bolt.testing import TestClient


def _make_text_api(chunks: list[str], *, compression=None) -> BoltAPI:
    api = BoltAPI(compression=compression) if compression is not None else BoltAPI()

    @api.get("/stream")
    async def stream():
        async def gen():
            for c in chunks:
                yield c
                await asyncio.sleep(0)

        return StreamingResponse(gen(), media_type="text/plain")

    return api


# ─── Default-on behavior ────────────────────────────────────────────────


def test_streaming_default_brotli_when_config_default():
    """`BoltAPI()` defaults to `CompressionConfig()` (brotli). Plain
    `StreamingResponse(...)` — no `compress=` kwarg, no per-route opt-in —
    must auto-compress for a client that advertises `br`.
    """
    chunks = ["alpha-", "beta-", "gamma-", "delta"]
    api = _make_text_api(chunks)
    with TestClient(api) as client:
        resp = client.get("/stream", headers={"Accept-Encoding": "br"})
        assert resp.status_code == 200
        assert resp.headers.get("content-encoding", "").lower() == "br"
        assert "accept-encoding" in resp.headers.get("vary", "").lower()
        # httpx auto-decodes brotli → content equals joined chunks.
        assert resp.content == "".join(chunks).encode()


def test_streaming_default_sse_when_config_default():
    """Default-on also applies to SSE: a bare `EventSourceResponse` must
    auto-compress (not the historical "identity hardcoded" behavior).
    """
    api = BoltAPI()

    @api.get("/feed")
    async def feed():
        async def gen():
            for i in range(5):
                yield {"i": i}

        return EventSourceResponse(gen())

    with TestClient(api) as client:
        resp = client.get("/feed", headers={"Accept-Encoding": "br"})
        assert resp.status_code == 200
        assert resp.headers.get("content-encoding", "").lower() == "br"
        # httpx decoded the brotli stream; verify SSE wire format.
        decoded = resp.content
        for i in range(5):
            assert f'data: {{"i": {i}}}\n\n'.encode() in decoded or f'data: {{"i":{i}}}\n\n'.encode() in decoded


# ─── @no_compress opt-out ───────────────────────────────────────────────


def test_streaming_no_compress_opt_out():
    """`@no_compress` on a streaming handler must skip the codec selection
    and emit no `Content-Encoding` (identity is stripped by the middleware).
    """
    api = BoltAPI()  # default CompressionConfig

    @api.get("/raw")
    @no_compress
    async def raw():
        async def gen():
            yield "no-compress-please"

        return StreamingResponse(gen(), media_type="text/plain")

    with TestClient(api) as client:
        resp = client.get("/raw", headers={"Accept-Encoding": "br"})
        ce = resp.headers.get("content-encoding", "").lower()
        # Middleware strips "identity" before sending.
        assert ce in ("", "identity")
        assert resp.content == b"no-compress-please"


# ─── BoltAPI(compression=False/None) ────────────────────────────────────


def test_streaming_no_compression_when_disabled():
    """`BoltAPI(compression=False)` disables compression for the whole API —
    streaming responses behave like `@no_compress` on every handler.
    """
    api = BoltAPI(compression=False)

    @api.get("/stream")
    async def stream():
        async def gen():
            yield "uncompressed"

        return StreamingResponse(gen(), media_type="text/plain")

    with TestClient(api) as client:
        resp = client.get("/stream", headers={"Accept-Encoding": "br"})
        ce = resp.headers.get("content-encoding", "").lower()
        assert ce in ("", "identity")
        assert resp.content == b"uncompressed"


# ─── Negotiation ────────────────────────────────────────────────────────


def test_streaming_gzip_fallback():
    """Backend brotli, but client only accepts gzip and `gzip_fallback=True`:
    the stream is gzip-encoded per chunk and httpx auto-decodes.
    """
    api = _make_text_api(["abc", "def", "ghi"], compression=CompressionConfig(backend="brotli"))
    with TestClient(api) as client:
        resp = client.get("/stream", headers={"Accept-Encoding": "gzip"})
        assert resp.headers.get("content-encoding", "").lower() == "gzip"
        # httpx decoded the gzip stream.
        assert resp.content == b"abcdefghi"


def test_streaming_no_negotiable_encoding_falls_back_to_identity():
    """Client only advertises an encoding we can't satisfy: stream uncompressed."""
    api = _make_text_api(["plain"], compression=CompressionConfig(backend="brotli", gzip_fallback=False))
    with TestClient(api) as client:
        resp = client.get("/stream", headers={"Accept-Encoding": "deflate"})
        ce = resp.headers.get("content-encoding", "").lower()
        assert ce in ("", "identity")
        assert resp.content == b"plain"


def test_streaming_zstd_when_configured():
    """`backend="zstd"` and the client accepts zstd → zstd-encoded stream."""
    pytest.importorskip("zstandard", reason="zstandard package required for httpx auto-decode")
    api = _make_text_api(["zstd-", "compressed"], compression=CompressionConfig(backend="zstd"))
    with TestClient(api) as client:
        resp = client.get("/stream", headers={"Accept-Encoding": "zstd"})
        assert resp.headers.get("content-encoding", "").lower() == "zstd"
        assert resp.content == b"zstd-compressed"


# ─── API surface — removed kwargs ───────────────────────────────────────


def test_streaming_response_rejects_removed_compress_kwarg():
    """Passing `compress=` raises `TypeError` — the kwarg was removed in
    favor of the `CompressionConfig`/`@no_compress` model.
    """
    async def gen():
        yield "x"

    with pytest.raises(TypeError):
        StreamingResponse(gen(), media_type="text/plain", compress="br")


def test_event_source_response_rejects_removed_compress_kwarg():
    async def gen():
        yield {"x": 1}

    with pytest.raises(TypeError):
        EventSourceResponse(gen(), compress="br")


# ─── Middleware bypass (no double compression) ──────────────────────────


def test_streaming_brotli_skips_global_compression():
    """Streaming compression runs inside the handler; the global compression
    middleware must bypass any pre-set `Content-Encoding`. httpx's auto-decode
    would produce garbage bytes if the middleware re-wrapped the body.
    """
    api = BoltAPI(compression=CompressionConfig(backend="brotli", minimum_size=1))

    @api.get("/stream")
    async def stream():
        async def gen():
            for v in ("one ", "two ", "three"):
                yield v

        return StreamingResponse(gen(), media_type="text/plain")

    with TestClient(api) as client:
        resp = client.get("/stream", headers={"Accept-Encoding": "br"})
        assert resp.headers.get("content-encoding", "").lower() == "br"
        assert resp.content == b"one two three"


def test_streaming_brotli_octet_stream():
    """Binary media types also auto-compress."""
    payload = [b"\x00\x01\x02", b"\xff\xfe\xfd", b"foobar"]
    api = BoltAPI()

    @api.get("/bin")
    async def bin_stream():
        async def gen():
            for chunk in payload:
                yield chunk

        return StreamingResponse(gen(), media_type="application/octet-stream")

    with TestClient(api) as client:
        resp = client.get("/bin", headers={"Accept-Encoding": "br"})
        assert resp.headers.get("content-encoding", "").lower() == "br"
        assert resp.content == b"".join(payload)
