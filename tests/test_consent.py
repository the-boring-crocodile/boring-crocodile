"""Consent uses stored request metadata; no live authority or credentials."""
import unittest
from html import escape
from urllib.parse import parse_qs, urlparse

from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

import test_service_routes as fixtures


class ApprovalPageTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ServiceRoutesTest.asyncSetUp
    start_worker = fixtures.ServiceRoutesTest.start_worker

    async def pending(self, name=None):
        client = OAuthClientInformationFull(
            client_id="untrusted-client", client_name=name, scope="mcp",
            token_endpoint_auth_method="none",
            redirect_uris=[AnyUrl("https://recipient.test/callback?label=one&next=two")])
        params = AuthorizationParams(
            state="synthetic-state", scopes=["mcp"], code_challenge="challenge",
            redirect_uri=client.redirect_uris[0], redirect_uri_provided_explicitly=True,
            resource="https://server.test/mcp/alpha")
        url = await self.provider.authorize(client, params)
        return parse_qs(urlparse(url).query)["request"][0], str(params.redirect_uri)

    async def test_approval_shows_stored_recipient_and_authority(self):
        request_id, callback = await self.pending('<img src=x onerror="bad()">')
        response = await self.http.get("/approve", params={"request": request_id})
        self.assertEqual(response.status_code, 200)
        self.assertIn("untrusted-client", response.text)
        self.assertIn(escape(callback), response.text)
        self.assertIn(escape('<img src=x onerror="bad()">'), response.text)
        self.assertNotIn('<img src=x', response.text)
        self.assertIn("registered itself; its name is not verified", response.text)
        self.assertIn("alpha", response.text)
        self.assertIn("commands", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])

    async def test_missing_name_and_expired_request_are_clear_and_private(self):
        request_id, _ = await self.pending()
        response = await self.http.get("/approve", params={"request": request_id})
        self.assertIn("no name given", response.text)
        unknown = await self.http.get("/approve", params={"request": "missing"})
        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(unknown.headers["cache-control"], "no-store")
        denied = await self.http.post("/approve", params={"request": request_id},
                                      data={"secret": "wrong", "pond": "alpha"})
        self.assertNotEqual(denied.status_code, 302)
        self.assertEqual(denied.headers["x-frame-options"], "DENY")
