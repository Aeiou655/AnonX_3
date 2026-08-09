"""Cache endpoint."""

import logging

from fastapi import APIRouter, Request

from AnonX_3.downloader_api.core.dependencies import RequestIdDep, ApiKeyDep
from AnonX_3.downloader_api.schemas.cache import CacheStatsResponse, CacheStats
from AnonX_3.downloader_api.cache.cache_manager import cache_manager

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/cache/stats", response_model=CacheStatsResponse)
async def get_cache_stats(
    request: Request,
    request_id: RequestIdDep,
    api_key: ApiKeyDep,
):
    stats = cache_manager.get_stats()

    return CacheStatsResponse(
        success=True,
        stats=stats,
        request_id=request_id,
    )
