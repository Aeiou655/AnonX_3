"""Error response schemas."""

from typing import Optional
from pydantic import BaseModel


class ErrorDetail(BaseModel):
    code: str
    message: str
    retryable: bool = False


class ErrorResponse(BaseModel):
    success: bool = False
    error: ErrorDetail
    request_id: str
    job_id: Optional[str] = None
