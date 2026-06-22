import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import init_db
from app.routers.auth import router as auth_router
from app.routers.cantrips import router as cantrips_router
from app.routers.diagnostics import router as diagnostics_router
from app.routers.endpoints import router as endpoints_router
from app.routers.forbidden_words import router as forbidden_words_router
from app.routers.lorebook import router as lorebook_router
from app.routers.memories import router as memories_router
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
    logger.info("GitInTheVan starting up")
    await init_db()
    yield
    logger.info("GitInTheVan shutting down")


app = FastAPI(
    title="GitInTheVan",
    description="Self-hostable MITM LLM router/proxy for roleplay services",
    version="0.11.4",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(proxy_router)
app.include_router(auth_router)
app.include_router(users_router)
app.include_router(endpoints_router)
app.include_router(settings_router)
app.include_router(lorebook_router)
app.include_router(memories_router)
app.include_router(cantrips_router)
app.include_router(diagnostics_router)
app.include_router(verification_router)
app.include_router(summarization_router)
app.include_router(forbidden_words_router)
app.include_router(packs_router)


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
