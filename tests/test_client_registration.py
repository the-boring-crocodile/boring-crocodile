"""Who may sign in to the Crocodile: metadata documents and admin clients; no open registration by default."""
import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx2
from boring_service_kit.issuer.clients import add_client
from boring_service_kit.issuer.store import Transaction

from boring_crocodile.control_plane import create_http_app
from boring_crocodile.settings import Settings
from pond_config import PROFILE_ID, one_pond_toml

CLAUDE = "https://claude.ai/oauth/test-client.json"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"


class ClientRegistrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        root.chmod(0o700)
        (root / "secret").write_text("pond-approval")
        (root / "config.toml").write_text(one_pond_toml(root))
        self.settings = Settings.load(root / "config.toml")
        self.mcp, self.app = create_http_app(self.settings)
        self.provider = self.mcp._auth_server_provider
        self.fetched = []

        def fetch(url):
            self.fetched.append(url)
            return json.dumps({"client_id": url, "client_name": "Claude", "redirect_uris": [CALLBACK],
                               "token_endpoint_auth_methods_supported": ["none"]}).encode()
        self.provider.fetch = fetch
        self.http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.app), base_url="https://example.test")
        self.addAsyncCleanup(self.http.aclose)

    async def sign_in(self, client_id, redirect, resource="https://example.test/mcp/pond"):
        verifier = "v" * 50
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        response = await self.http.get("/authorize", params={
            "response_type": "code", "client_id": client_id, "redirect_uri": redirect, "code_challenge": challenge,
            "code_challenge_method": "S256", "state": "s", "resource": resource, "scope": "mcp"})
        self.assertEqual(response.status_code, 302, response.text)
        query = urlsplit(response.headers["location"]).query
        page = (await self.http.get("/approve?" + query)).text
        approved = await self.http.post("/approve?" + query, data={"secret": "pond-approval"})
        self.assertEqual(approved.status_code, 302, approved.text)
        code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
        token = await self.http.post("/token", data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect, "client_id": client_id,
            "code_verifier": verifier, "resource": resource})
        self.assertEqual(token.status_code, 200, token.text)
        return page, token.json()

    async def test_metadata_offers_documents_not_registration(self):
        metadata = (await self.http.get("/.well-known/oauth-authorization-server")).json()
        self.assertTrue(metadata["client_id_metadata_document_supported"])
        self.assertIn("none", metadata["token_endpoint_auth_methods_supported"])
        self.assertNotIn("registration_endpoint", metadata)
        self.assertEqual((await self.http.post("/register", json={"redirect_uris": [CALLBACK]})).status_code, 404)

    async def test_claude_signs_in_with_its_document(self):
        page, tokens = await self.sign_in(CLAUDE, CALLBACK)
        self.assertIn('<p class="to">claude.ai</p>', page)
        self.assertIn("Pond: <strong>The Pond</strong>", page)
        access = await self.provider.load_access_token(tokens["access_token"])
        self.assertEqual(access.subject, PROFILE_ID)
        self.assertEqual(self.fetched, [CLAUDE])

    async def test_admin_client_for_vs_code(self):
        with Transaction(self.settings.oauth_database_file) as db:
            added = add_client(db, "VS Code", ["http://127.0.0.1/"])
        page, tokens = await self.sign_in(added["client_id"], "http://127.0.0.1:33418/")
        self.assertIn("added by this server's administrator", page)
        self.assertTrue(tokens["access_token"].startswith("access_"))
