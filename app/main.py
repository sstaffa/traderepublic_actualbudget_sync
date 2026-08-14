from contextlib import asynccontextmanager
import base64
import logging
import os
import secrets

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
from pathlib import Path
from app.api.routes import router as api_router
from app.core.config import settings
from app.core.i18n import get_language, reset_language, set_language
from app.services.scheduler import start_scheduler, stop_scheduler

# Without this, log.info() calls from the app's own modules are dropped: the
# root logger defaults to WARNING, so only uvicorn and pytr (which configure
# their own handlers) show up in `docker logs`. That hides scheduler timings,
# sync results and non-fatal warnings exactly when they are needed for
# debugging. Override with LOG_LEVEL=DEBUG for more detail.
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Clear out sessions left behind by earlier login attempts before anything
    # else runs; a failure here must never prevent startup.
    try:
        from app.services.trade_republic import prune_sessions

        prune_sessions()
    except Exception:
        logging.getLogger(__name__).warning("Session pruning at startup failed", exc_info=True)

    scheduler_task = start_scheduler()
    try:
        yield
    finally:
        await stop_scheduler(scheduler_task)


app = FastAPI(title="TR → Actual Sync (backend)", lifespan=lifespan)


@app.middleware("http")
async def request_language(request: Request, call_next):
    token = set_language(request.headers.get("Accept-Language"))
    try:
        response = await call_next(request)
        response.headers["Content-Language"] = get_language()
        if request.url.path in {"/", "/static/i18n.js"}:
            response.headers["Cache-Control"] = "no-store, max-age=0"
        return response
    finally:
        reset_language(token)


@app.middleware("http")
async def optional_basic_auth(request: Request, call_next):
    username = settings.basic_auth_username
    password = settings.basic_auth_password
    if not username and not password:
        return await call_next(request)
    if request.url.path == "/health":
        return await call_next(request)

    auth = request.headers.get("Authorization", "")
    scheme, _, credentials = auth.partition(" ")
    valid = False
    if scheme.lower() == "basic" and credentials:
        try:
            decoded = base64.b64decode(credentials).decode("utf-8")
            provided_user, _, provided_password = decoded.partition(":")
            valid = (
                secrets.compare_digest(provided_user, username)
                and secrets.compare_digest(provided_password, password)
            )
        except Exception:
            valid = False

    if not valid:
        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="TR Actual Sync"'},
        )
    return await call_next(request)

app.include_router(api_router, prefix="", tags=["tr-sync"])

# Mount static files for a minimal UI
static_dir = Path(__file__).resolve().parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/")
async def index():
    index_file = static_dir / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return {"status": "ok"}


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)