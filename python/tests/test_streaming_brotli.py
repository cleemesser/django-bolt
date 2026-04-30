"""End-to-end tests for per-chunk brotli compression on non-SSE StreamingResponse.

These mirror the SSE coverage in test_sse_brotli.py but exercise the generic
StreamingResponse path (e.g. text/plain, application/octet-stream) so we
verify that the brotli option is wired through for any streaming media type,
not just text/event-stream.

httpx auto-decodes Content-Encoding: br, so resp.content is the *decoded*
plain bytes. Tests:
  1. Assert Content-Encoding: br to confirm wire compression was applied.
  2. Inspect resp.content (auto-decoded) to verify chunk content.
"""

from __future__ import annotations

import asyncio

import pytest

brotli = pytest.importorskip("brotli", reason="brotli or brotlicffi package not installed")

from django_bolt import BoltAPI
from django_bolt.responses import StreamingResponse
from django_bolt.testing import TestClient


def _make_text_api(chunks: list[str], *, compress: str | None = None) -> BoltAPI:
    api = BoltAPI()

    @api.get("/stream")
    async def stream():
        async def gen():
            for c in chunks:
                yield c
                await asyncio.sleep(0)

        return StreamingResponse(gen(), media_type="text/plain", compress=compress)

    return api


def test_streaming_brotli_response_headers():
    api = _make_text_api(["hello "], compress="br")
    with TestClient(api) as client:
        resp = client.get("/stream", headers={"Accept-Encoding": "br"})
        assert resp.status_code == 200
        assert resp.headers.get("content-encoding", "").lower() == "br"
        assert "accept-encoding" in resp.headers.get("vary", "").lower()


def test_streaming_brotli_negotiation_fallback():
    # Client doesn't advertise br — server falls back to identity.
    api = _make_text_api(["hello"], compress="br")
    with TestClient(api) as client:
        resp = client.get("/stream", headers={"Accept-Encoding": "gzip"})
        assert resp.status_code == 200
        ce = resp.headers.get("content-encoding", "").lower()
        assert ce in ("", "identity")
        # Body is plain (un-compressed) text.
        assert resp.content == b"hello"


def test_streaming_brotli_decodes_to_original_bytes():
    chunks = ["alpha-", "beta-", "gamma-", "delta"]
    api = _make_text_api(chunks, compress="br")
    with TestClient(api) as client:
        resp = client.get("/stream", headers={"Accept-Encoding": "br"})
        assert resp.headers.get("content-encoding", "").lower() == "br"
        # httpx decoded the brotli stream — content must equal the joined chunks.
        assert resp.content == "".join(chunks).encode()


def test_streaming_brotli_skips_global_compression():
    # When global brotli middleware is enabled AND per-chunk brotli is opted
    # in on the response, the body must NOT be double-compressed. httpx's
    # auto-decode would produce garbage bytes if the middleware re-wrapped.
    from django_bolt.middleware import CompressionConfig

    api = BoltAPI(compression=CompressionConfig(backend="brotli", minimum_size=1))

    @api.get("/stream")
    async def stream():
        async def gen():
            for v in ("one ", "two ", "three"):
                yield v

        return StreamingResponse(gen(), media_type="text/plain", compress="br")

    with TestClient(api) as client:
        resp = client.get("/stream", headers={"Accept-Encoding": "br"})
        assert resp.headers.get("content-encoding", "").lower() == "br"
        assert resp.content == b"one two three"


def test_streaming_brotli_octet_stream():
    # Binary media types also work — prove the path isn't tied to text.
    payload = [b"\x00\x01\x02", b"\xff\xfe\xfd", b"foobar"]
    api = BoltAPI()

    @api.get("/bin")
    async def bin_stream():
        async def gen():
            for chunk in payload:
                yield chunk

        return StreamingResponse(gen(), media_type="application/octet-stream", compress="br")

    with TestClient(api) as client:
        resp = client.get("/bin", headers={"Accept-Encoding": "br"})
        assert resp.headers.get("content-encoding", "").lower() == "br"
        assert resp.content == b"".join(payload)
