"""Local tunneling client for animax-player backend.

This client polls the backend tunnel endpoints and proxies the backend's
request jobs through your local machine.

Run locally:
    python client.py --backend-url https://animax-player.vercel.app
"""

import argparse
import asyncio
import base64
import json
import logging
import uuid
from typing import Any, Dict, Optional

import httpx

BACKOFF_INITIAL = 1.5
BACKOFF_MAX = 30.0
POLL_TIMEOUT = 25.0
CLIENT_TIMEOUT = 30.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("animax-tunnel-client")


async def handle_request(
    backend_base: str,
    client: httpx.AsyncClient,
    message: Dict[str, Any],
) -> None:
    request_id = message.get("id")
    payload: Dict[str, Any] = {
        "id": request_id,
        "status_code": 500,
        "headers": {},
        "body": "",
    }

    try:
        body_b64 = message.get("body", "")
        content: Optional[bytes] = None
        if body_b64:
            content = base64.b64decode(body_b64)

        response = await client.request(
            method=message.get("method", "GET"),
            url=message.get("url", ""),
            headers=message.get("headers", {}),
            content=content,
        )

        payload["status_code"] = response.status_code
        payload["headers"] = dict(response.headers)
        payload["body"] = base64.b64encode(response.content).decode("ascii")
    except Exception as exc:
        payload["error"] = str(exc)

    try:
        await client.post(
            f"{backend_base.rstrip('/')}/tunnel/response",
            json=payload,
            timeout=CLIENT_TIMEOUT,
        )
    except Exception as exc:
        logger.warning("Failed to send tunnel response %s: %s", request_id, exc)


async def poll_loop(backend_base: str) -> None:
    client_id = uuid.uuid4().hex
    backoff = BACKOFF_INITIAL

    async with httpx.AsyncClient(
        timeout=CLIENT_TIMEOUT,
        follow_redirects=True,
        trust_env=False,
        http2=True,
    ) as client:
        while True:
            try:
                logger.info("Polling backend tunnel at %s", backend_base)
                response = await client.get(
                    f"{backend_base.rstrip('/')}/tunnel/poll",
                    params={"client_id": client_id},
                    timeout=POLL_TIMEOUT + 5,
                )
                if response.status_code != 200:
                    raise RuntimeError(f"Backend poll failed: {response.status_code}")

                payload = response.json()
                if payload.get("type") == "request":
                    await handle_request(backend_base, client, payload)
                    continue

                await asyncio.sleep(1.0)
                backoff = BACKOFF_INITIAL
            except Exception as exc:
                logger.warning("Tunnel polling error: %s", exc)
                logger.info("Reconnecting in %.1f seconds...", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Animax local tunnel client")
    parser.add_argument(
        "--backend-url",
        default="https://animax-s21j.onrender.com",
        help="HTTP base URL for the animax-player backend tunnel",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    try:
        asyncio.run(poll_loop(args.backend_url))
    except KeyboardInterrupt:
        logger.info("Tunnel client interrupted and exiting")
