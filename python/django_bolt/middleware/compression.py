"""
Compression configuration for Django-Bolt.

Provides configuration options for response compression (gzip, brotli, zstd).
Compression levels are handled automatically by Actix Web with optimized defaults.
"""

from dataclasses import dataclass
from typing import Literal


# Static type aliases for the per-codec tuning fields. Type checkers
# (mypy/pyright) flag obvious out-of-range literals; `_check_int_range`
# still enforces the same ranges at runtime for values that arrive
# dynamically (config files, env vars, msgspec-decoded dicts, etc.).
BrotliQuality = Literal[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
BrotliLgWin = Literal[10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24]
GzipLevel = Literal[0, 1, 2, 3, 4, 5, 6, 7, 8, 9]
ZstdLevel = Literal[
    1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11,
    12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22,
]


def _check_int_range(name: str, value: object, lo: int, hi: int) -> None:
    # `bool` is an `int` subclass — reject it explicitly so True/False can't
    # sneak through as 0/1 (a footgun for tuning fields).
    if not isinstance(value, int) or isinstance(value, bool) or not (lo <= value <= hi):
        raise ValueError(f"{name} must be an int in [{lo}, {hi}], got {value!r}")


@dataclass
class CompressionConfig:
    """Configuration for response compression.

    Applies to both buffered and streaming responses. Streaming responses
    (``StreamingResponse``, ``EventSourceResponse``) use a per-chunk flush
    encoder so events still arrive at the client one at a time. Buffered
    responses go through the global compression middleware.

    Per-route opt-out: ``@no_compress``.

    Args:
        backend: Compression backend to use (default: "brotli").
            One of "gzip", "brotli", "zstd".
        minimum_size: Minimum buffered-response size in bytes to compress
            (default: 500). Has no effect on streaming responses.
        gzip_fallback: Use gzip if the client doesn't accept the configured
            backend (default: True). Applies to streaming responses too.
        brotli_quality: Brotli quality 0..=11 (default: 5).
        brotli_lgwin: Brotli sliding window log size 10..=24 (default: 14 →
            16 KiB window). Dominant per-connection memory knob for streams.
        gzip_level: Gzip compression level 0..=9 (default: 6).
        zstd_level: Zstd compression level 1..=22 (default: 3).

    Examples:
        # Defaults — brotli, gzip fallback, on for every response (buffered + streaming)
        api = BoltAPI(compression=CompressionConfig())

        # Smaller window for high-fanout SSE servers
        api = BoltAPI(compression=CompressionConfig(brotli_lgwin=12))

        # Gzip backend, no fallback
        api = BoltAPI(compression=CompressionConfig(
            backend="gzip",
            gzip_fallback=False,
            gzip_level=6,
        ))

        # Disable compression entirely
        api = BoltAPI(compression=None)
    """

    backend: Literal["gzip", "brotli", "zstd"] = "brotli"
    minimum_size: int = 500
    gzip_fallback: bool = True
    brotli_quality: BrotliQuality = 5
    brotli_lgwin: BrotliLgWin = 14
    gzip_level: GzipLevel = 6
    zstd_level: ZstdLevel = 3

    def __post_init__(self):
        valid_backends = {"gzip", "brotli", "zstd"}
        if self.backend not in valid_backends:
            raise ValueError(f"Invalid backend: {self.backend}. Must be one of {valid_backends}")
        if self.minimum_size < 0:
            raise ValueError("minimum_size must be non-negative")
        _check_int_range("brotli_quality", self.brotli_quality, 0, 11)
        _check_int_range("brotli_lgwin", self.brotli_lgwin, 10, 24)
        _check_int_range("gzip_level", self.gzip_level, 0, 9)
        _check_int_range("zstd_level", self.zstd_level, 1, 22)

    def to_rust_config(self) -> dict:
        return {
            "backend": self.backend,
            "minimum_size": self.minimum_size,
            "gzip_fallback": self.gzip_fallback,
            "brotli_quality": self.brotli_quality,
            "brotli_lgwin": self.brotli_lgwin,
            "gzip_level": self.gzip_level,
            "zstd_level": self.zstd_level,
        }
