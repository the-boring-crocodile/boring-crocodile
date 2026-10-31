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

    async def sign_in(self, client_id, redirect, resource="https://example.test/mcp/pond",
                      client_secret=None, headers=None):
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
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect, "client_id": client_id,
                "code_verifier": verifier, "resource": resource}
        if client_secret is not None:
            form["client_secret"] = client_secret
        token = await self.http.post("/token", data=form, headers=headers)
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

    async def start_transport(self):
        from async_lifespan import start_lifespan
        await start_lifespan(self, self.app.router.lifespan_context(self.app))

    async def profile_status(self, token):
        from boring_service_kit.testing import mcp_headers, mcp_meta
        return await self.http.post("/mcp/pond",
            headers=mcp_headers("tools/call", "pond_profile", token),
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "pond_profile", "arguments": {}, "_meta": mcp_meta()}})

    async def public_revocation(self, client_id, redirect):
        await self.start_transport()
        _, tokens = await self.sign_in(client_id, redirect)
        with Transaction(self.settings.oauth_database_file) as db:
            other = add_client(db, "Other public client", [CALLBACK])["client_id"]
        _, kept = await self.sign_in(other, CALLBACK)
        before = await self.profile_status(tokens["access_token"])
        self.assertEqual(before.status_code, 200)
        self.assertEqual(before.json()["result"]["structuredContent"]["id"], PROFILE_ID)
        form = {"client_id": client_id, "token": tokens["refresh_token"],
                "token_type_hint": "refresh_token"}
        # A public client cannot revoke another client's grant.
        wrong = await self.http.post("/revoke", data={**form, "client_id": other})
        self.assertEqual(wrong.status_code, 200)
        self.assertEqual((await self.profile_status(tokens["access_token"])).status_code, 200)
        revoked = await self.http.post("/revoke", data=form)
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertEqual((await self.profile_status(tokens["access_token"])).status_code, 401)
        refresh = await self.http.post("/token", data={
            "grant_type": "refresh_token", "client_id": client_id, "refresh_token": tokens["refresh_token"]})
        self.assertEqual(refresh.status_code, 400)
        self.assertEqual(refresh.json()["error"], "invalid_grant")
        self.assertEqual((await self.profile_status(kept["access_token"])).status_code, 200)
        self.assertEqual((await self.http.post("/revoke", data=form)).status_code, 200)
        self.assertEqual((await self.http.post("/revoke", data={**form, "token": "unknown"})).status_code, 200)
        metadata = (await self.http.get("/.well-known/oauth-authorization-server")).json()
        self.assertIn("none", metadata["revocation_endpoint_auth_methods_supported"])

    async def test_metadata_client_can_revoke_without_secret(self):
        await self.public_revocation(CLAUDE, CALLBACK)
        self.assertEqual(self.fetched, [CLAUDE])

    async def test_admin_public_client_can_revoke_without_secret(self):
        with Transaction(self.settings.oauth_database_file) as db:
            client = add_client(db, "Public native client", ["http://127.0.0.1/callback"])["client_id"]
        await self.public_revocation(client, "http://127.0.0.1:33418/callback")

    async def confidential_revocation(self, auth):
        await self.start_transport()
        with Transaction(self.settings.oauth_database_file) as db:
            added = add_client(db, "Confidential client", [CALLBACK], auth=auth)
        client, secret = added["client_id"], added["client_secret"]
        def headers(value):
            return {"Authorization": "Basic " + base64.b64encode(f"{client}:{value}".encode()).decode()}
        _, tokens = await self.sign_in(client, CALLBACK,
            client_secret=secret if auth == "client_secret_post" else None,
            headers=headers(secret) if auth == "client_secret_basic" else None)
        form = {"client_id": client, "token": tokens["access_token"], "token_type_hint": "access_token"}
        for wrong in (None, "", "wrong"):
            data, head = dict(form), {}
            if wrong is not None:
                if auth == "client_secret_post":
                    data["client_secret"] = wrong
                else:
                    head = headers(wrong)
            refused = await self.http.post("/revoke", data=data, headers=head)
            self.assertEqual(refused.status_code, 401)
            self.assertNotIn(secret, refused.text)
            self.assertEqual((await self.profile_status(tokens["access_token"])).status_code, 200)
        data = dict(form)
        if auth == "client_secret_post":
            data["client_secret"] = secret
        head = headers(secret) if auth == "client_secret_basic" else {}
        revoked = await self.http.post("/revoke", data=data, headers=head)
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertEqual((await self.profile_status(tokens["access_token"])).status_code, 401)
        refresh_form = {"grant_type": "refresh_token", "client_id": client,
                        "refresh_token": tokens["refresh_token"]}
        if auth == "client_secret_post":
            refresh_form["client_secret"] = secret
        refresh = await self.http.post("/token", data=refresh_form, headers=head)
        self.assertEqual(refresh.status_code, 400)
        self.assertEqual(refresh.json()["error"], "invalid_grant")

    async def test_basic_client_revocation_preserves_authentication(self):
        await self.confidential_revocation("client_secret_basic")

    async def test_post_client_revocation_preserves_authentication(self):
        await self.confidential_revocation("client_secret_post")
