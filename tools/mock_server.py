"""Offline mock of the relay endpoints - useful for testing without credits.

    python tools/mock_server.py --port 8765
    python main.py probe --base-url http://127.0.0.1:8765
    python main.py gen item_potion_icon --base-url http://127.0.0.1:8765 -v
"""

from __future__ import annotations

import argparse
import base64
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover - Pillow is optional
    Image = None

TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)


def sample_png(size: int = 256, background=(0, 255, 0), body=(200, 40, 40)) -> bytes:
    """A flat-background test image: green border, red ellipse in the middle."""
    if Image is None:
        return base64.b64decode(TINY_PNG_B64)
    image = Image.new("RGB", (size, size), background)
    draw = ImageDraw.Draw(image)
    draw.ellipse(
        [size * 0.2, size * 0.15, size * 0.8, size * 0.85],
        fill=body,
        outline=(20, 20, 20),
        width=4,
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # keep the console readable
        print("mock: " + (fmt % args), flush=True)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization") or self.headers.get("x-goog-api-key") or ""
        return "sk-" in header

    def _send(self, status: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/v1/models":
            if not self._authorized():
                return self._send(401, {"error": {"message": "invalid token"}})
            return self._send(
                200,
                {
                    "data": [
                        {"id": "gemini-3.1-flash-image"},
                        {"id": "gemini-3-pro-image"},
                        {"id": "gpt-4o-mini"},
                    ]
                },
            )
        self._send(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            payload = {}
        if not self._authorized():
            return self._send(401, {"error": {"message": "invalid token"}})

        encoded = base64.b64encode(sample_png()).decode("ascii")
        print(
            "mock: model="
            + str(payload.get("model", "?"))
            + " keys="
            + ",".join(sorted(payload))
        , flush=True)

        if self.path.startswith("/v1beta/models/"):
            return self._send(
                200,
                {
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": [
                                    {"text": "Here is your image."},
                                    {"inlineData": {"mimeType": "image/png", "data": encoded}},
                                ],
                            },
                            "finishReason": "STOP",
                        }
                    ]
                },
            )

        if self.path.startswith("/v1/chat/completions"):
            return self._send(
                200,
                {
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "![image](data:image/png;base64," + encoded + ")",
                            },
                            "finish_reason": "stop",
                        }
                    ]
                },
            )

        self._send(404, {"error": {"message": "not found"}})


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock relay server for offline testing.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print("mock relay listening on http://%s:%s" % (args.host, args.port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped")


if __name__ == "__main__":
    main()
