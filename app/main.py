import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import init_db
from app.routers.admin import router as admin_router
from app.routers.api_keys import router as api_keys_router
from app.routers.audit import router as audit_router
from app.routers.auth import router as auth_router
from app.routers.cantrips import router as cantrips_router
from app.routers.debug import router as debug_router
from app.routers.diagnostics import router as diagnostics_router
from app.routers.endpoints import router as endpoints_router
from app.routers.forbidden_words import router as forbidden_words_router
from app.routers.lorebook import router as lorebook_router
from app.routers.maps import router as maps_router
from app.routers.memories import router as memories_router
from app.routers.memory_rules import router as memory_rules_router
from app.routers.packs import router as packs_router
from app.routers.proxy import router as proxy_router
from app.routers.settings import router as settings_router
from app.routers.summarization import router as summarization_router
from app.routers.users import router as users_router
from app.routers.verification import router as verification_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from app.services.log_manager import setup_file_logging
    setup_file_logging()
    logger.info("GitInTheVan starting up")
    await init_db()
    yield
    logger.info("GitInTheVan shutting down")


app = FastAPI(
    title="GitInTheVan",
    description="Self-hostable MITM LLM router/proxy for roleplay services",
    version="0.14.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=settings.cors_origins != "*",
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_size_limit(request: Request, call_next):
    content_length = request.headers.get("content-length", "")
    if content_length and content_length.isdigit():
        if int(content_length) > settings.max_request_body_size:
            return JSONResponse(
                status_code=413,
                content={"detail": "Request body too large"},
            )
    return await call_next(request)


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    from app.services.rate_limiter import check_api_rate_limit, check_proxy_rate_limit

    path = request.url.path
    if path.startswith("/api/"):
        try:
            await check_api_rate_limit(request)
        except HTTPException as e:
            return JSONResponse(status_code=e.status_code, content={"detail": e.detail})
    elif path not in ("/", "/health") and not path.startswith(("/help", "/assets", "/app", "/gitinthevan")):
        try:
            await check_proxy_rate_limit(request)
        except HTTPException as e:
            return JSONResponse(status_code=e.status_code, content={"detail": e.detail})

    return await call_next(request)


app.include_router(auth_router)
app.include_router(users_router)
app.include_router(endpoints_router)
app.include_router(api_keys_router)
app.include_router(settings_router)
app.include_router(lorebook_router)
app.include_router(maps_router)
app.include_router(memories_router)
app.include_router(memory_rules_router)
app.include_router(cantrips_router)
app.include_router(debug_router)
app.include_router(diagnostics_router)
app.include_router(verification_router)
app.include_router(summarization_router)
app.include_router(forbidden_words_router)
app.include_router(packs_router)
app.include_router(audit_router)
app.include_router(admin_router)


@app.get("/health")
async def health_check():
    return {"status": "ok"}


_static_dir = Path(__file__).resolve().parent.parent / "static"
if _static_dir.exists() and (_static_dir / "index.html").exists():
    app.mount("/assets", StaticFiles(directory=_static_dir / "assets"), name="assets")

    @app.get("/")
    @app.get("/app")
    @app.get("/app/{path:path}")
    async def serve_spa(path: str = ""):
        return FileResponse(_static_dir / "index.html")

    @app.get("/gitinthevan-full.svg")
    @app.get("/gitinthevan-logoonly.svg")
    async def serve_static_file(request: Request):
        filename = request.url.path.lstrip("/")
        file_path = _static_dir / filename
        if file_path.exists():
            return FileResponse(file_path)
        raise HTTPException(status_code=404)


_docs_dir = Path(__file__).resolve().parent.parent / "docs"
if _docs_dir.exists():
    app.mount("/help", StaticFiles(directory=_docs_dir, html=True), name="docs")

# Proxy catch-all MUST be registered last so management API, health,
# static files, and docs routes are matched first.
app.include_router(proxy_router)
