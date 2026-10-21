"""Synthetic disposable-guest OAuth probe; never point at a production site.

Creates one synthetic client/grant in the guest DB. Secrets remain in memory.
Uses loopback transport while preserving the discovered public resource identity.
"""
import argparse
import base64
import hashlib
import http.client
import json
from pathlib import Path
import secrets
from urllib.parse import urlencode, urlsplit, parse_qs


def run(base_url, approval_file, pond, emit=print):
    base = urlsplit(base_url)
    if base.scheme != "http" or base.hostname not in ("127.0.0.1", "::1") or base.path not in ("", "/"):
        raise ValueError("Synthetic guest probe requires an HTTP loopback origin")
    stages = []
    def request(stage, method, path, expected, data=None, form=False, token=None):
        headers = {"Accept": "application/json, text/event-stream"}
        payload = None
        if data is not None:
            payload = urlencode(data) if form else json.dumps(data)
            headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
        if token:
            headers["Authorization"] = "Bearer " + token
            headers["MCP-Protocol-Version"] = "2025-03-26"
        connection = http.client.HTTPConnection(base.hostname, base.port, timeout=10)
        try:
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            body = response.read()
            try:
                decoded = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                decoded = None
            record = {"stage": stage, "status": response.status}
            if response.status not in expected:
                # Do not emit response bodies, URLs with codes, or token values.
                record["challenge_present"] = bool(response.getheader("WWW-Authenticate"))
                error = decoded.get("error") if isinstance(decoded, dict) else None
                if error in ("invalid_token", "invalid_request", "invalid_grant", "invalid_client",
                             "invalid_client_metadata", "insufficient_scope", "invalid_scope"):
                    record["error"] = error
            emit(json.dumps(record))
            stages.append(record)
            if response.status not in expected:
                raise RuntimeError("Failed at " + stage + "; inspect safe guest diagnostics")
            return decoded, dict(response.getheaders())
        finally:
            connection.close()

    metadata, _ = request("resource_metadata", "GET",
                          f"/.well-known/oauth-protected-resource/mcp/{pond}", (200,))
    resource = metadata["resource"]
    if urlsplit(resource).scheme != "https":
        raise ValueError("Expected public HTTPS resource in metadata")
    emit(json.dumps({"resource": resource}))
    registered, _ = request("registration", "POST", "/register", (201,), {
        "redirect_uris": ["http://127.0.0.1:48999/callback"],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "scope": "mcp"})
    client = registered["client_id"]
    verifier = secrets.token_urlsafe(48)
    state = secrets.token_urlsafe(16)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    _, headers = request("authorize", "GET", "/authorize?" + urlencode({
        "client_id": client, "response_type": "code", "scope": "mcp",
        "redirect_uri": "http://127.0.0.1:48999/callback", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
        "resource": resource}), (302,))
    location = urlsplit(next(v for k, v in headers.items() if k.lower() == "location"))
    if location.path != "/approve":
        raise RuntimeError("Unexpected approval route")
    request_id = parse_qs(location.query)["request"][0]
    _, headers = request("approval", "POST", "/approve?" + urlencode({"request": request_id}),
        (302,), {"secret": Path(approval_file).read_text().strip()}, form=True)
    callback = parse_qs(urlsplit(next(v for k, v in headers.items() if k.lower() == "location")).query)
    if callback.get("state") != [state]:
        raise RuntimeError("Callback state mismatch")
    issued, _ = request("token", "POST", "/token", (200,), {
        "grant_type": "authorization_code", "client_id": client,
        "redirect_uri": "http://127.0.0.1:48999/callback", "code": callback["code"][0],
        "code_verifier": verifier, "resource": resource}, form=True)
    token = issued["access_token"]
    for number, (stage, method, params) in enumerate([
        ("initialize", "initialize", {"protocolVersion": "2025-03-26",
            "capabilities": {}, "clientInfo": {"name": "synthetic-guest-probe", "version": "1"}}),
        ("tools_list", "tools/list", {}),
        ("pond_profile", "tools/call", {"name": "pond_profile", "arguments": {}}),
    ], 1):
        result, _ = request(stage, "POST", f"/mcp/{pond}", (200,), {
            "jsonrpc": "2.0", "id": number, "method": method, "params": params}, token=token)
        if not isinstance(result, dict) or "error" in result or result.get("result", {}).get("isError"):
            raise RuntimeError("MCP result failed at " + stage)
    return stages


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--synthetic-approval-file", type=Path, required=True)
    parser.add_argument("--pond", required=True)
    args = parser.parse_args()
    try:
        run(args.base_url, args.synthetic_approval_file, args.pond)
    except Exception as error:
        # Exceptions from parsers/HTTP can contain sensitive response fragments.
        print(json.dumps({"outcome": "failed", "exception_type": type(error).__name__}))
        raise SystemExit(1)
    print('{"outcome": "authenticated_profile_passed"}')
