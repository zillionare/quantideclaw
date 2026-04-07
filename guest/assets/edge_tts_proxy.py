#!/usr/bin/env python3
"""Minimal OpenAI-compatible speech endpoint backed by edge-tts."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import edge_tts

HOST = os.environ.get("EDGE_TTS_PROXY_HOST", "127.0.0.1")
PORT = int(os.environ.get("EDGE_TTS_PROXY_PORT", "18792"))
DEFAULT_VOICE = os.environ.get("EDGE_TTS_VOICE", "zh-CN-XiaoxiaoNeural")


async def synthesize_to_file(text: str, voice: str, output_path: Path) -> None:
    communicate = edge_tts.Communicate(text=text, voice=voice)
    await communicate.save(str(output_path))


class Handler(BaseHTTPRequestHandler):
    server_version = "OpenClawEdgeTTS/1.0"

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/health":
            self._send_json(200, {"status": "ok", "voice": DEFAULT_VOICE})
            return
        self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/v1/audio/speech":
            self._send_json(404, {"error": "not_found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid_json"})
            return

        text = str(payload.get("input") or "").strip()
        voice = str(payload.get("voice") or DEFAULT_VOICE).strip() or DEFAULT_VOICE
        if not text:
            self._send_json(400, {"error": "missing_input"})
            return

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as handle:
            output_path = Path(handle.name)

        try:
            asyncio.run(synthesize_to_file(text, voice, output_path))
            audio = output_path.read_bytes()
        except Exception as exc:  # pragma: no cover - runtime path only
            self._send_json(500, {"error": "tts_failed", "detail": str(exc)})
            return
        finally:
            output_path.unlink(missing_ok=True)

        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(audio)))
        self.end_headers()
        self.wfile.write(audio)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()