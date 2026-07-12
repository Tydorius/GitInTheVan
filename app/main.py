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
from app.routers.scenario_rules import router as scenario_rules_router
from app.routers.settings import router as settings_router
from app.routers.skills import router as skills_router
from app.routers.summarization import router as summarization_router
from app.routers.tag_groups import router as tag_groups_router
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

    try:
        import importlib.metadata

        importlib.metadata.version("litellm")
        logger.info("LiteLLM %s loaded", importlib.metadata.version("litellm"))
    except ImportError:
        logger.warning(
            "LiteLLM not installed. Provider-based endpoints (Gemini, OpenRouter, etc.) will fail. "
            "Run: pip install litellm==1.89.4"
        )

    if settings.cors_origins == "*" and (settings.behind_proxy or not settings.generate_certs):
        logger.warning(
            "CORS is set to allow all origins (GITV_CORS_ORIGINS=*) while running in a "
            "non-local deployment mode (GITV_BEHIND_PROXY=%s, GITV_GENERATE_CERTS=%s). "
            "This is fine for local/LAN use but if this instance is reachable from the "
            "public internet, set GITV_CORS_ORIGINS to your actual client origin(s).",
            settings.behind_proxy,
            settings.generate_certs,
        )

    from app.services.firewall_check import check_firewall
    check_firewall(settings.port)
    await init_db()
    yield
    logger.info("GitInTheVan shutting down")


app = FastAPI(
    title="GitInTheVan",
    description="Self-hostable MITM LLM router/proxy for roleplay services",
    version="0.16.1",
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
app.include_router(skills_router)
app.include_router(scenario_rules_router)
app.include_router(tag_groups_router)
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


if __name__ == "__main__":
    import atexit
    import os
    import signal
    import sys

    import uvicorn

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Single-instance lock via PID file
    _pid_file = Path(__file__).resolve().parent.parent / "data" / "gitv.pid"
    _pid_file.parent.mkdir(parents=True, exist_ok=True)

    def _is_process_running(pid: int) -> bool:
        if sys.platform == "win32":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            synchronize = 0x00100000
            handle = kernel32.OpenProcess(synchronize, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        else:
            try:
                os.kill(pid, 0)
                return True
            except (ProcessLookupError, PermissionError):
                return False

    if _pid_file.exists():
        try:
            old_pid = int(_pid_file.read_text().strip())
            if _is_process_running(old_pid):
                print(f"ERROR: GitInTheVan is already running (PID {old_pid}).", file=sys.stderr)
                print(f"If this is incorrect, delete {_pid_file} and try again.", file=sys.stderr)
                sys.exit(1)
        except (ValueError, OSError):
            pass
        _pid_file.unlink(missing_ok=True)

    _pid_file.write_text(str(os.getpid()))

    def _cleanup_pid_file():
        _pid_file.unlink(missing_ok=True)

    atexit.register(_cleanup_pid_file)

    def _signal_handler(signum, frame):
        _cleanup_pid_file()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    ssl_kwargs = {}
    _use_ssl = False
    if settings.ssl_certfile and settings.ssl_keyfile:
        cert_path = Path(settings.ssl_certfile)
        key_path = Path(settings.ssl_keyfile)
        if cert_path.exists() and key_path.exists():
            ssl_kwargs = {
                "ssl_certfile": str(cert_path),
                "ssl_keyfile": str(key_path),
            }
            _use_ssl = True
            logger.info("Starting with HTTPS (cert: %s)", cert_path)
        else:
            logger.warning(
                "SSL cert/key not found (%s, %s), starting without HTTPS",
                cert_path, key_path,
            )
    else:
        logger.info("Starting with HTTP (no SSL configured)")

    redirect_thread = None
    if _use_ssl and settings.http_redirect_port > 0:
        import asyncio
        import threading
        from http import HTTPStatus

        async def _redirect_handler(reader, writer):
            try:
                request_line = await reader.readline()
                req_host = None
                while True:
                    header = await reader.readline()
                    if header in (b"\r\n", b"\n", b""):
                        break
                    decoded = header.decode(errors="replace").strip().lower()
                    if decoded.startswith("host:"):
                        req_host = decoded[5:].strip()
                parts = request_line.decode().split()
                path = parts[1] if len(parts) > 1 else "/"

                if req_host:
                    if ":" in req_host:
                        req_host = req_host.rsplit(":", 1)[0]
                    redirect_url = f"https://{req_host}:{settings.port}{path}"
                else:
                    redirect_url = f"https://localhost:{settings.port}{path}"

                body = f'<html><body>Redirecting to <a href="{redirect_url}">{redirect_url}</a></body></html>'
                response = (
                    f"HTTP/1.1 {HTTPStatus.MOVED_PERMANENTLY.value} {HTTPStatus.MOVED_PERMANENTLY.phrase}\r\n"
                    f"Location: {redirect_url}\r\n"
                    f"Content-Type: text/html\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    f"Connection: close\r\n"
                    f"\r\n{body}"
                )
                writer.write(response.encode())
                await writer.drain()
            except Exception:
                pass
            finally:
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

        def _run_redirect_server():
            loop = asyncio.new_event_loop()
            try:
                coro = asyncio.start_server(_redirect_handler, settings.host, settings.http_redirect_port)
                loop.run_until_complete(coro)
                logger.info("HTTP redirect server on port %d → https://:%d", settings.http_redirect_port, settings.port)
            except OSError as e:
                logger.warning(
                    "Could not bind HTTP redirect port %d (%s). "
                    "Run as administrator/root for port 80, or set GITV_HTTP_REDIRECT_PORT to 0 to suppress.",
                    settings.http_redirect_port, e,
                )
            loop.run_forever()

        redirect_thread = threading.Thread(target=_run_redirect_server, daemon=True)
        redirect_thread.start()

    uvicorn_kwargs: dict = {"host": settings.host, "port": settings.port, "log_level": settings.log_level.lower()}
    uvicorn_kwargs.update(ssl_kwargs)

    if settings.behind_proxy:
        uvicorn_kwargs["proxy_headers"] = True
        uvicorn_kwargs["forwarded_allow_ips"] = "*"
        logger.info("Behind proxy mode: trusting X-Forwarded-* headers")

    uvicorn.run(
        "app.main:app",
        **uvicorn_kwargs,
    )
