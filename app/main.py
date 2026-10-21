"""FastAPI application: SalesEasy Copilot.

Note: no ``from __future__ import annotations`` here. FastAPI must resolve the
request model, which is defined inside ``create_app`` so its max length can
come from settings; postponed annotations would turn it into a query param.
"""

import json
import re
import uuid
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .config import Settings, load_settings
from .llm import FAILURE_MODES, LLMClient, build_llm_client
from .logging_setup import hash_client, setup_logging
from .pipeline import TriagePipeline
from .ratelimit import RateLimiter, client_key
from .schemas import APP_VERSION, TriageResult
from .verified_data import get_verified_data

STATIC_DIR = Path(__file__).parent / "static"
SCENARIOS_PATH = Path(__file__).parent / "data" / "scenarios.json"

# Control characters, zero-width characters and bidi overrides are removed before
# analysis: they can hide instructions from keyword checks ("ig​nore").
_INVISIBLE_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁠-⁤⁦-⁩﻿]")

_PAGE_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


def sanitize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _INVISIBLE_RE.sub("", text).strip()


class BodySizeLimitMiddleware:
    """Reject request bodies above ``max_bytes`` before they are parsed (HTTP 413)."""

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                too_big = int(declared) > self.max_bytes
            except ValueError:
                too_big = True
            if too_big:
                await self._reject(send)
                return

        received = 0

        class _TooLarge(Exception):
            pass

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _TooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _TooLarge:
            await self._reject(send)

    async def _reject(self, send):
        body = b'{"error":"payload_too_large","detail":"Request body is too large."}'
        await send({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})


def create_app(settings: Settings | None = None, llm: LLMClient | None = None) -> FastAPI:
    settings = settings or load_settings()
    logger = setup_logging(settings.log_level)
    llm = llm or build_llm_client(settings)
    pipeline = TriagePipeline(llm, get_verified_data())
    limiter = RateLimiter(settings.rate_limit_per_minute, settings.daily_request_cap)
    is_mock = getattr(llm, "provider", "") == "mock"

    app = FastAPI(
        title="SalesEasy Copilot",
        version=APP_VERSION,
        description="Triage FMCG field-sales updates into validated, evidence-grounded JSON with human-review escalation. Fictional data only.",
    )
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_body_bytes)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response

    class AnalyzeRequest(BaseModel):
        model_config = ConfigDict(extra="forbid")

        text: str = Field(
            min_length=1,
            max_length=settings.max_input_chars,
            description="Plain-text field update from a sales rep (untrusted input).",
            examples=["Metro Fresh has a stockout on Cola 500ml - no bottles on the shelf."],
        )
        simulate_failure: Optional[Literal[FAILURE_MODES]] = Field(  # type: ignore[valid-type]
            default=None,
            description="Demo only: simulate an LLM failure. Disabled when ALLOW_FAILURE_SIMULATION=false.",
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        details = [
            {"field": ".".join(str(p) for p in err.get("loc", ()) if p != "body"), "message": err.get("msg", "invalid")}
            for err in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"error": "invalid_request", "detail": details})

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        request_id = getattr(request.state, "request_id", None)
        logger.error("unhandled error", exc_info=exc, extra={"fields": {"request_id": request_id}})
        return JSONResponse(status_code=500, content={"error": "internal_error", "request_id": request_id})

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Content-Security-Policy": _PAGE_CSP})

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/health", tags=["ops"])
    def health():
        return {"status": "ok", "version": APP_VERSION, "provider": llm.provider, "model": llm.model}

    @app.get("/api/config", tags=["ops"])
    def config():
        return {
            "version": APP_VERSION,
            "provider": llm.provider,
            "model": llm.model,
            "mock_mode": is_mock,
            "allow_failure_simulation": settings.allow_failure_simulation,
            "failure_modes": list(FAILURE_MODES),
            "max_input_chars": settings.max_input_chars,
            "rate_limit_per_minute": settings.rate_limit_per_minute,
            "verified_data": get_verified_data().as_public_list(),
        }

    @app.get("/api/schema", tags=["contract"])
    def schema():
        return TriageResult.model_json_schema()

    scenarios = json.loads(SCENARIOS_PATH.read_text(encoding="utf-8"))

    @app.get("/api/samples", tags=["ops"])
    def samples():
        """Sample field updates: the five scenarios from the brief plus added test cases."""
        return scenarios

    @app.post("/api/analyze", response_model=TriageResult, tags=["triage"])
    def analyze(body: AnalyzeRequest, request: Request):
        request_id = uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        key = client_key(
            request.client.host if request.client else None,
            request.headers.get("x-forwarded-for"),
            settings.trusted_proxy_hops,
        )
        decision = limiter.check(key)
        if not decision.allowed:
            logger.info("rate_limited", extra={"fields": {"request_id": request_id, "client": hash_client(key)}})
            raise HTTPException(status_code=429, detail=decision.reason, headers={"Retry-After": str(decision.retry_after_s)})
        if body.simulate_failure and not settings.allow_failure_simulation:
            raise HTTPException(status_code=403, detail="Failure simulation is disabled on this deployment.")

        text = sanitize_text(body.text)
        if len(text) < 3:
            raise HTTPException(status_code=422, detail="The field update is empty after removing control characters.")

        result = pipeline.run(text, request_id=request_id, simulate_failure=body.simulate_failure)
        logger.info(
            "analyzed",
            extra={
                "fields": {
                    "request_id": request_id,
                    "client": hash_client(key),
                    "input_chars": len(text),
                    "provider": result.meta.provider,
                    "model": result.meta.model,
                    "simulated_failure": body.simulate_failure,
                    "degraded": result.meta.degraded,
                    "attempts": result.meta.llm_attempts,
                    "category": result.issue_category.value,
                    "urgency": result.urgency.value,
                    "needs_human_review": result.needs_human_review,
                    "rules": result.meta.rules_triggered,
                    "latency_ms": result.meta.latency_ms,
                }
            },
        )
        return result

    logger.info(
        "startup",
        extra={"fields": {"provider": llm.provider, "model": llm.model, "version": APP_VERSION, "mock_mode": is_mock}},
    )
    return app


app = create_app()
