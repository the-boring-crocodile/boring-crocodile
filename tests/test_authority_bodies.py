"""Authority route admission bounds bytes and time before effects."""
import asyncio
import unittest
from unittest.mock import AsyncMock

from starlette.requests import Request
from boring_crocodile.http_bodies import BodyRefused, bounded_body
import test_service_routes as fixtures


class BodyReaderTest(unittest.IsolatedAsyncioTestCase):
    async def test_stream_without_length_is_bounded(self):
        chunks = iter([{"type": "http.request", "body": b"1234", "more_body": True},
                       {"type": "http.request", "body": b"5678", "more_body": False}])
        async def receive():
            return next(chunks)
        request = Request({"type": "http", "headers": []}, receive)
        with self.assertRaises(BodyRefused) as caught:
            await bounded_body(request, 7)
        self.assertEqual(caught.exception.status, 413)

    async def test_declared_oversize_never_reads_and_stall_is_cancelled(self):
        receive = AsyncMock()
        request = Request({"type": "http", "headers": [(b"content-length", b"8")]}, receive)
        with self.assertRaises(BodyRefused):
            await bounded_body(request, 7)
        receive.assert_not_awaited()
        closed = asyncio.Event()
        async def stall():
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
        request = Request({"type": "http", "headers": []}, stall)
        with self.assertRaises(BodyRefused) as caught:
            await bounded_body(request, 7, timeout=.01)
        self.assertEqual(caught.exception.status, 408)
        self.assertTrue(closed.is_set())


class AuthorityBodyTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ServiceRoutesTest.asyncSetUp
    start_worker = fixtures.ServiceRoutesTest.start_worker

    async def test_internal_auth_precedes_body_admission(self):
        huge = b"x" * 20000
        for path in ("/services/register", "/introspect"):
            response = await self.http.post(path, content=huge)
            self.assertEqual(response.status_code, 401)
            authenticated = await self.http.post(path, content=huge, headers={
                "Authorization": fixtures.basic(self.canvas["client_id"], self.canvas["client_secret"])})
            self.assertEqual(authenticated.status_code, 413)
        self.assertEqual(self.requests, {"alpha": [], "beta": []})

    async def test_approval_rejects_oversized_stream(self):
        async def chunks():
            yield b"x" * 9000
            yield b"x" * 9000
        response = await self.http.post("/approve", content=chunks())
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.headers["cache-control"], "no-store")
