from __future__ import annotations

# Server-integration smoke test for per-event brotli SSE compression.
#
# This test runs runbolt as a real subprocess and makes requests over a real TCP
# socket, catching startup-time wiring issues that TestClient (in-process) tests
# miss.
#
# NOTE on httpx brotli auto-decode:
#   httpx.Client automatically decodes Content-Encoding: br responses, so
#   resp.content is always the *decoded* plain-text SSE wire bytes.  Tests:
#     1. Assert Content-Encoding: br to confirm wire compression was applied.
#     2. Inspect resp.content (auto-decoded) to verify SSE payload correctness.
#   We do NOT call brotli.decompress() manually — httpx already did it.

import pytest

# httpx auto-decodes Content-Encoding: br only when the `brotli` package is
# installed in the test environment. Without it, resp.content is the raw
# compressed bytes and the SSE-format assertion below fails. Skip cleanly
# rather than fail noisily.
pytest.importorskip("brotli", reason="brotli package required for httpx auto-decode")

from tests.server_integration.helpers import create_server_project  # noqa: E402


@pytest.mark.server_integration
def test_brotli_sse_end_to_end_via_real_server(tmp_path):
    """Verify brotli SSE works through the real runbolt server entry point."""
    project = create_server_project(
        tmp_path,
        project_api_body="""
        from django_bolt.responses import EventSourceResponse

        @api.get("/events")
        async def stream():
            async def gen():
                for i in range(5):
                    yield {"i": i}
            return EventSourceResponse(gen(), compress="br")
        """,
    )

    with project.start(startup_path="/health") as server:
        # httpx.Client (in RunningServer) auto-decodes brotli.
        resp = server.get("/events", headers={"Accept-Encoding": "br"})

    # 1. Confirm wire-level brotli compression was applied.
    assert resp.status_code == 200
    assert resp.headers.get("content-encoding", "").lower() == "br"

    # 2. httpx decoded the brotli stream; verify the SSE wire format is correct.
    decoded = resp.content
    for i in range(5):
        assert f'data: {{"i": {i}}}\n\n'.encode() in decoded or f'data: {{"i":{i}}}\n\n'.encode() in decoded, (
            f"event i={i} not found in decoded SSE stream"
        )
