import logging

from fastapi import APIRouter, Request

from app.services.proxy import forward_request

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    return await forward_request(request)


@router.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def forward_generic(path: str, request: Request):
    return await forward_request(request)


@router.api_route("/v1beta/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def forward_generic_v1beta(path: str, request: Request):
    return await forward_request(request)
