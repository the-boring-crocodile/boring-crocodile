"""Bound custom authority form bodies before parsing; never retain partial secrets."""
import asyncio

from starlette.requests import Request


class BodyRefused(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


async def bounded_body(request: Request, limit: int, timeout: float = 5.0) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None:
        if not declared.isascii() or not declared.isdigit() or len(declared) > 12:
            raise BodyRefused(400, "invalid_request", "invalid Content-Length")
        if int(declared) > limit:
            raise BodyRefused(413, "limit", "request body exceeds route limit")

    async def read() -> bytes:
        result = bytearray()
        async for chunk in request.stream():
            if len(chunk) > limit - len(result):
                raise BodyRefused(413, "limit", "request body exceeds route limit")
            result.extend(chunk)
        return bytes(result)

    try:
        return await asyncio.wait_for(read(), timeout)
    except asyncio.TimeoutError:
        raise BodyRefused(408, "limit", "request body deadline exceeded") from None
