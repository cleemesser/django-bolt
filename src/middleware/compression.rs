use actix_http::encoding::Encoder;
/// Custom compression middleware that respects Content-Encoding: identity header
/// and CompressionConfig settings.
///
/// This middleware checks response headers BEFORE applying compression.
/// If Content-Encoding: identity is present (set when skip_compression=true),
/// it removes the header and returns the response uncompressed.
/// Otherwise, it applies compression based on CompressionConfig and Accept-Encoding.
use actix_web::{
    body::{BodySize, MessageBody},
    dev::{forward_ready, Service, ServiceRequest, ServiceResponse, Transform},
    http::header::{ContentEncoding, ACCEPT_ENCODING, CONTENT_ENCODING, VARY},
    Error,
};
use futures_util::future::LocalBoxFuture;
use std::future::{ready, Ready};
use std::sync::Arc;

use crate::metadata::CompressionConfig;
use crate::state::AppState;

/// Compression middleware factory
pub struct CompressionMiddleware;

impl CompressionMiddleware {
    pub fn new() -> Self {
        Self
    }
}

impl<S, B> Transform<S, ServiceRequest> for CompressionMiddleware
where
    S: Service<ServiceRequest, Response = ServiceResponse<B>, Error = Error> + 'static,
    S::Future: 'static,
    B: MessageBody + 'static,
{
    type Response = ServiceResponse<Encoder<B>>;
    type Error = Error;
    type InitError = ();
    type Transform = CompressionMiddlewareService<S>;
    type Future = Ready<Result<Self::Transform, Self::InitError>>;

    fn new_transform(&self, service: S) -> Self::Future {
        ready(Ok(CompressionMiddlewareService { service }))
    }
}

pub struct CompressionMiddlewareService<S> {
    service: S,
}

impl<S, B> Service<ServiceRequest> for CompressionMiddlewareService<S>
where
    S: Service<ServiceRequest, Response = ServiceResponse<B>, Error = Error> + 'static,
    S::Future: 'static,
    B: MessageBody + 'static,
{
    type Response = ServiceResponse<Encoder<B>>;
    type Error = Error;
    type Future = LocalBoxFuture<'static, Result<Self::Response, Self::Error>>;

    forward_ready!(service);

    fn call(&self, req: ServiceRequest) -> Self::Future {
        // Store Accept-Encoding header from request for later use
        let accept_encoding = req
            .headers()
            .get(ACCEPT_ENCODING)
            .and_then(|v| v.to_str().ok())
            .map(|s| s.to_string());

        // Get compression config from app state
        let compression_config = req
            .app_data::<actix_web::web::Data<Arc<AppState>>>()
            .and_then(|state| state.global_compression_config.clone());

        let fut = self.service.call(req);

        Box::pin(async move {
            let res = fut.await?;

            // If the response already declares a Content-Encoding, the handler
            // owns the encoding (e.g. per-event brotli SSE). Pass it through
            // without re-compressing.
            //
            // Special case: `identity` is our internal "skip compression"
            // marker — we strip it before sending so clients don't see it.
            // Any other value (br, gzip, zstd, etc.) is preserved as-is.
            let pre_set_encoding = res
                .headers()
                .get(CONTENT_ENCODING)
                .and_then(|v| v.to_str().ok())
                .map(|v| v.to_ascii_lowercase());

            if let Some(encoding) = pre_set_encoding {
                let (req, mut response) = res.into_parts();
                if encoding == "identity" {
                    response.headers_mut().remove(CONTENT_ENCODING);
                }
                // For any pre-set encoding (identity or otherwise), pass the
                // body through with ContentEncoding::Identity so actix doesn't
                // wrap it in another encoder.
                return Ok(ServiceResponse::new(
                    req,
                    response.map_body(|head, body| {
                        Encoder::response(ContentEncoding::Identity, head, body)
                    }),
                ));
            }

            {
                // Apply compression based on CompressionConfig and Accept-Encoding
                let (req, mut response) = res.into_parts();

                // Select encoding based on config and client support
                let encoding =
                    select_encoding(accept_encoding.as_deref(), compression_config.as_ref());

                // Get minimum size from config or use default
                let minimum_size = compression_config
                    .as_ref()
                    .map(|c| c.minimum_size)
                    .unwrap_or(500);

                // Check if response size warrants compression (skip small responses)
                let should_compress = match response.body().size() {
                    BodySize::None => encoding != ContentEncoding::Identity,
                    BodySize::Sized(size) => {
                        size >= minimum_size as u64 && encoding != ContentEncoding::Identity
                    }
                    _ => encoding != ContentEncoding::Identity,
                };

                if should_compress {
                    // Add Vary header to indicate content varies by Accept-Encoding
                    // Use append (not insert) to preserve existing Vary headers like CORS's Vary: Origin
                    response.headers_mut().append(
                        VARY,
                        actix_web::http::header::HeaderValue::from_static("accept-encoding"),
                    );

                    // Create encoder with selected encoding
                    Ok(ServiceResponse::new(
                        req,
                        response.map_body(|head, body| Encoder::response(encoding, head, body)),
                    ))
                } else {
                    // No compression needed
                    Ok(ServiceResponse::new(
                        req,
                        response.map_body(|head, body| {
                            Encoder::response(ContentEncoding::Identity, head, body)
                        }),
                    ))
                }
            }
        })
    }
}

#[cfg(test)]
mod bypass_tests {
    use super::*;
    use actix_web::http::header::{HeaderName, HeaderValue};
    use actix_web::test::{self, TestRequest};
    use actix_web::{web, App, HttpResponse};

    #[actix_web::test]
    async fn pre_set_brotli_encoding_is_preserved_and_not_double_compressed() {
        // The body must be >= minimum_size (default 500 bytes) to trigger the
        // compression path in the middleware. With a short body the middleware
        // skips compression regardless, which would hide the bug we are testing.
        let large_body: Vec<u8> = b"already-compressed-bytes-".iter().cycle().take(600).cloned().collect();
        let large_body_clone = large_body.clone();

        let app = test::init_service(
            App::new().wrap(CompressionMiddleware::new()).route(
                "/",
                web::get().to(move || {
                    let body = large_body_clone.clone();
                    async move {
                        HttpResponse::Ok()
                            .insert_header((
                                HeaderName::from_static("content-encoding"),
                                HeaderValue::from_static("br"),
                            ))
                            .body(body)
                    }
                }),
            ),
        )
        .await;

        let req = TestRequest::get()
            .uri("/")
            .insert_header(("accept-encoding", "br, gzip"))
            .to_request();

        let resp = test::call_service(&app, req).await;
        assert_eq!(resp.status(), 200);

        let ce = resp
            .headers()
            .get("content-encoding")
            .map(|v| v.to_str().unwrap().to_string());
        assert_eq!(ce, Some("br".to_string()));

        // Body must be passed through unchanged; the middleware must NOT
        // wrap it in another encoder.
        let body = test::read_body(resp).await;
        assert_eq!(body.as_ref(), large_body.as_slice());
    }
}

/// Select best compression encoding based on config and client support
fn select_encoding(
    accept_encoding: Option<&str>,
    config: Option<&CompressionConfig>,
) -> ContentEncoding {
    let ae = match accept_encoding {
        Some(ae) => ae,
        None => return ContentEncoding::Identity,
    };

    // Get preferred backend from config or use default (brotli)
    let preferred_backend = config.map(|c| c.backend.as_str()).unwrap_or("brotli");

    let gzip_fallback = config.map(|c| c.gzip_fallback).unwrap_or(true);

    // Check if client supports preferred backend
    let client_supports_preferred = match preferred_backend {
        "brotli" => ae.contains("br"),
        "gzip" => ae.contains("gzip"),
        "zstd" => ae.contains("zstd"),
        _ => false,
    };

    if client_supports_preferred {
        // Use preferred backend if client supports it
        match preferred_backend {
            "brotli" => ContentEncoding::Brotli,
            "gzip" => ContentEncoding::Gzip,
            "zstd" => ContentEncoding::Zstd,
            _ => ContentEncoding::Identity,
        }
    } else if gzip_fallback && ae.contains("gzip") {
        // Fall back to gzip if enabled and client supports it
        ContentEncoding::Gzip
    } else {
        // No compression
        ContentEncoding::Identity
    }
}
