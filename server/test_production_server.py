import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import main
import production_server
from http.server import ThreadingHTTPServer


class ProductionBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mktemp(suffix=".json"))
        self.env = patch.dict(os.environ, {
            "SECONDHELLO_ENV": "production",
            "SECONDHELLO_AUTH_TOKEN": "test-token",
            "SECONDHELLO_MEMORY_FILE": str(self.path),
            "MONGODB_URI": "",
            "MONGODB_PASSWORD": "",
            "FIREWORKS_API_KEY": "",
            "OPENROUTER_API_KEY": "",
            "ELEVENLABS_API_KEY": "",
            "ELEVENLABS_AGENT_ID": "",
            "SECONDHELLO_PROVIDER": "",
        })
        self.env.start()
        main.PROVIDER = main.Provider()
        main.BACKEND = main.MemoryBackend()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), production_server.SecondHelloHandler)
        self.server.config = production_server.configuration()
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.env.stop()
        self.path.unlink(missing_ok=True)

    def request(self, path, method="GET", payload=None, token=None, extra_headers=None):
        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(f"{self.base}{path}", data=body, method=method)
        if body is not None:
            request.add_header("Content-Type", "application/json")
        if token is not None:
            request.add_header("Authorization", f"Bearer {token}")
        for key, value in (extra_headers or {}).items():
            request.add_header(key, value)
        return urllib.request.urlopen(request, timeout=5)

    def test_health_is_public_but_memory_requires_bearer_token(self):
        with self.request("/api/health") as response:
            self.assertEqual(response.status, 200)
            self.assertTrue(json.loads(response.read())["ok"])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/memory")
        self.assertEqual(error.exception.code, 401)

    def test_authenticated_workflow_and_sse_have_terminal_events(self):
        payload = {
            "action": "draft",
            "introduction": {
                "recipientName": "Alex", "connectorName": "Jordan",
                "need": "applied ML", "offer": "climate expertise",
            },
        }
        with self.request("/api/workflow", "POST", payload, "test-token") as response:
            result = json.loads(response.read())
        self.assertTrue(result["draft"]["subject"])
        self.assertTrue(result["requestId"])

        with self.request("/api/workflow/events", "POST", payload, "test-token") as response:
            stream = response.read().decode()
        self.assertIn("event: workflow.started", stream)
        self.assertIn("event: workflow.completed", stream)

    def test_scribe_token_requires_authentication(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/voice/scribe-token", "POST", {}, None)
        self.assertEqual(error.exception.code, 401)

        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/voice/scribe-token", "POST", {}, "test-token")
        self.assertEqual(error.exception.code, 503)
        self.assertEqual(json.loads(error.exception.read())["reason"], "elevenlabs_api_key_required")

    def test_delete_requires_explicit_confirmation(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/memory", "DELETE", token="test-token")
        self.assertEqual(error.exception.code, 428)
        with self.request("/api/memory", "DELETE", token="test-token", extra_headers={"X-SecondHello-Confirm": "DELETE_ALL"}) as response:
            self.assertTrue(json.loads(response.read())["deleted"])


if __name__ == "__main__":
    unittest.main()
