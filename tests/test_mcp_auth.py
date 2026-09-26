from __future__ import annotations

import asyncio

from horus.mcp_server.server import BearerTokenASGI


def test_mcp_rejects_missing_or_invalid_bearer_token_before_dispatch():
    invoked = False

    async def downstream(scope, receive, send):
        nonlocal invoked
        invoked = True

    app = BearerTokenASGI(downstream, "correct-secret")

    async def request(headers):
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await app({"type": "http", "headers": headers}, receive, send)
        return messages

    missing = asyncio.run(request([]))
    wrong = asyncio.run(request([(b"authorization", b"Bearer wrong-secret")]))

    assert missing[0]["status"] == 401
    assert wrong[0]["status"] == 401
    assert (b"www-authenticate", b"Bearer") in missing[0]["headers"]
    assert not invoked
