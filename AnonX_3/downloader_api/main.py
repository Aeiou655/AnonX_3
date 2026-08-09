"""FastAPI application entry point."""

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
import time

from AnonX_3.downloader_api import __version__, __app_name__
from AnonX_3.downloader_api.lifespan import lifespan
from AnonX_3.downloader_api.api.router import router
from AnonX_3.downloader_api.core.error_handlers import register_error_handlers
from AnonX_3.downloader_api.core.dependencies import get_request_id


def create_app() -> FastAPI:
    app = FastAPI(
        title=__app_name__,
        version=__version__,
        description="Self-Hosted Downloader API for audio and video downloads",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def add_request_id_header(request: Request, call_next):
        request_id = await get_request_id(request)
        start_time = time.time()

        response = await call_next(request)

        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time"] = f"{(time.time() - start_time) * 1000:.2f}ms"

        return response

    register_error_handlers(app)

    app.include_router(router)

    @app.get("/", tags=["root"])
    async def root():
        return {
            "name": __app_name__,
            "version": __version__,
            "status": "running",
        }

    @app.get("/ping", tags=["root"])
    async def ping():
        return {"pong": True}

    return app


app = create_app()
