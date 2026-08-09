"""Main API router."""

from fastapi import APIRouter

from AnonX_3.downloader_api.api.v1 import download, metadata, jobs, cache, health, metrics, admin

router = APIRouter(prefix="/api/v1")

router.include_router(download.router, tags=["download"])
router.include_router(metadata.router, tags=["metadata"])
router.include_router(jobs.router, tags=["jobs"])
router.include_router(cache.router, tags=["cache"])
router.include_router(health.router, tags=["health"])
router.include_router(metrics.router, tags=["metrics"])
router.include_router(admin.router, tags=["admin"])
