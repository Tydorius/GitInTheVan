import logging

from fastapi import APIRouter, Request

from app.services.proxy import forward_request

logger = logging.getLogger(__name__)

router = APIRouter()


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def forward_generic(request: Request):
    return await forward_request(request)
