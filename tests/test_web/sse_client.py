"""Exercise real ASGI streaming and HTTP commands without buffering an infinite body."""

from __future__ import annotations

import asyncio
import json
from queue import Queue
from typing import Any

import httpx


class SSEClient:
    def __init__(self, client, session_id: str, headers=None):
        self.client = client
        self.session_id = session_id
        self.headers = headers or {"host": "localhost", "origin": "http://localhost"}
        self.frames = Queue(maxsize=1024)
        self.connection_id = ""
        self.buffer = ""
        self.status_code = 0
        self.response_headers = {}

    def __enter__(self):
        self.closed = self.client.portal.call(asyncio.Event)

        async def receive():
            await self.closed.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            self.frames.put_nowait(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": f"/api/sessions/{self.session_id}/events",
            "query_string": b"",
            "raw_path": f"/api/sessions/{self.session_id}/events".encode(),
            "root_path": "",
            "server": ("localhost", 80),
            "client": ("testclient", 123),
            "headers": [(k.encode(), v.encode()) for k, v in self.headers.items()],
        }
        self.task = self.client.portal.start_task_soon(self.client.app, scope, receive, send)
        started = self.frames.get(timeout=10)
        assert started["type"] == "http.response.start"
        self.status_code = started["status"]
        self.response_headers = {k.decode(): v.decode() for k, v in started["headers"]}
        if self.status_code >= 400:
            self.__exit__(None, None, None)
            raise httpx.HTTPStatusError(
                "SSE rejected",
                request=httpx.Request("GET", "http://localhost"),
                response=httpx.Response(self.status_code),
            )
        return self

    def __exit__(self, *args):
        self.client.portal.call(self.closed.set)
        self.task.result(timeout=10)

    def receive_json(self) -> dict[str, Any]:
        while True:
            if "\n\n" in self.buffer:
                frame, self.buffer = self.buffer.split("\n\n", 1)
                data = next((line[6:] for line in frame.split("\n") if line.startswith("data: ")), None)
                if data is not None:
                    event = json.loads(data)
                    if event["type"] == "ready":
                        self.connection_id = event["connection_id"]
                    return event
                continue
            message = self.frames.get(timeout=10)
            if message.get("body"):
                self.buffer += message["body"].decode()
            elif message.get("more_body") is False:
                raise EOFError("SSE stream ended")

    def command(self, payload: dict[str, Any]) -> httpx.Response:
        return self.client.post(
            f"/api/sessions/{self.session_id}/commands",
            json=payload,
            headers={**self.headers, "X-ResearchX-Connection": self.connection_id},
        )

    def send_json(self, payload: dict[str, Any]) -> None:
        response = self.command(payload)
        response.raise_for_status()
        assert response.status_code == 202
        assert response.json()["accepted"] is True


def sse_connect(client, session_id: str, headers=None):
    return SSEClient(client, session_id, headers)


class RecordingChannel:
    """Immutable recording sink for focused controller projection tests."""

    def __init__(self, events):
        self.events = events
        self.detached = asyncio.Event()

    async def publish(self, event):
        self.events.append(json.loads(json.dumps(event)))

    def detach(self):
        self.detached.set()
