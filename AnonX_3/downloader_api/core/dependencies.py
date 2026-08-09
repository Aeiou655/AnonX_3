"""FastAPI dependencies."""

import uuid
import time
import logging
from typing import Optional, Annotated

from fastapi import Depends, Header, Query, Request

from AnonX_3.downloader_api.core.config import settings
from AnonX_3.downloader_api.core.security import validate_api_key
from AnonX_3.downloader_api.core.exceptions import InvalidAPIKeyError

logger = logging.getLogger(__name__)


async def get_request_id(request: Request) -> str:
    request_id = request.headers.get("X-Request-ID")
    if not request_id:
        request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    request.state.start_time = time.time()
    return request_id


async def verify_api_key(
    request: Request,
    x_api_key: Annotated[Optional[str], Header()] = None,
    api_key: Annotated[Optional[str], Query()] = None,
) -> str:
    key = x_api_key or api_key
    if not key:
        raise InvalidAPIKeyError("API key is required")
    validate_api_key(key, require_admin=False)
    return key


async def verify_admin_api_key(
    request: Request,
    x_api_key: Annotated[Optional[str], Header()] = None,
    api_key: Annotated[Optional[str], Query()] = None,
) -> str:
    key = x_api_key or api_key
    if not key:
        raise InvalidAPIKeyError("Admin API key is required")
    validate_api_key(key, require_admin=True)
    return key


RequestIdDep = Annotated[str, Depends(get_request_id)]
ApiKeyDep = Annotated[str, Depends(verify_api_key)]
AdminApiKeyDep = Annotated[str, Depends(verify_admin_api_key)]
