import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mktemp(suffix=".json"))
        os.environ["SECONDHELLO_MEMORY_FILE"] = str(self.path)
        os.environ["MONGODB_URI"] = ""
        os.environ["MONGODB_PASSWORD"] = ""
        os.environ["FIREWORKS_API_KEY"] = ""
        os.environ["OPENROUTER_API_KEY"] = ""
        os.environ["ELEVENLABS_API_KEY"] = ""
        os.environ["SECONDHELLO_PROVIDER"] = ""
        main.PROVIDER = main.Provider()
        main.BACKEND = main.MemoryBackend()

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def capture(self, person_id, name, email, transcript):
        return main.run_workflow({
            "action": "capture",
            "person": {"id": person_id, "name": name, "email": email, "createdAt": "2026-08-13T12:00:00Z"},
            "conversation": {"id": f"conversation-{person_id}", "personID": person_id, "timestamp": "2026-08-13T12:00:00Z", "consented": True, "consentedAt": "2026-08-13T12:00:00Z", "transcript": transcript, "profile": {}},
        })

    def test_consent_gate_blocks_every_tool(self):
        result = main.run_workflow({"action": "capture", "person": {"id": "private", "name": "Private"}, "conversation": {"consented": False, "transcript": "Do not store this"}})
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "explicit_consent_required")
        self.assertFalse(self.path.exists())
        self.assertEqual([item["tool"] for item in result["trace"]], ["consent_gate"])

    def test_capture_calls_extract_persist_and_match_tools(self):
        result = self.capture("p1", "Alex", "alex@example.com", "I need an applied ML partner. I can offer climate domain expertise.")
        self.assertTrue(result["ok"])
        self.assertEqual([item["tool"] for item in result["trace"]], ["consent_gate", "extract_memory", "persist_memory", "find_introductions"])
        self.assertEqual(main.BACKEND.load()["people"][0]["name"], "Alex")

    def test_semantic_match_has_no_named_demo_branch(self):
        self.capture("p1", "Alex", "alex@example.com", "I need an applied ML partner. I can offer climate domain expertise.")
        result = self.capture("p2", "Jordan", "jordan@example.com", "I can offer applied ML architecture. I need climate domain expertise.")
        self.assertGreaterEqual(len(result["opportunities"]), 1)
        first = result["opportunities"][0]
        self.assertIn(first["recipientName"], {"Alex", "Jordan"})
        self.assertGreaterEqual(first["score"], 0.18)

    def test_draft_is_editable_and_not_sent(self):
        result = main.run_workflow({"action": "draft", "introduction": {"id": "idea", "recipientName": "Alex", "recipientEmail": "alex@example.com", "connectorName": "Jordan", "connectorEmail": "jordan@example.com", "need": "an ML partner", "offer": "ML architecture"}})
        self.assertEqual(result["draft"]["to"], "alex@example.com")
        self.assertIn("Would you like to connect?", result["draft"]["body"])
        self.assertIn("nothing was sent", result["trace"][-1]["detail"].lower())

    def test_private_voice_url_requires_server_credentials(self):
        status, payload = main.elevenlabs_signed_url()
        self.assertEqual(status, 503)
        self.assertEqual(payload["reason"], "elevenlabs_api_key_and_agent_id_required")

    def test_private_voice_url_uses_server_key_without_returning_it(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self): return b'{"signed_url":"wss://example.invalid/private-session"}'

        os.environ["ELEVENLABS_API_KEY"] = "server-only-test-key"
        os.environ["ELEVENLABS_AGENT_ID"] = "agent-test"
        with patch.object(main.urllib.request, "urlopen", return_value=Response()) as urlopen:
            status, payload = main.elevenlabs_signed_url()

        request = urlopen.call_args.args[0]
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True, "signedUrl": "wss://example.invalid/private-session"})
        self.assertEqual(request.get_header("Xi-api-key"), "server-only-test-key")
        self.assertIn("agent_id=agent-test", request.full_url)
        self.assertNotIn("server-only-test-key", str(payload))

    def test_provider_requires_explicit_key_and_model(self):
        os.environ["FIREWORKS_API_KEY"] = "configured-for-test"
        os.environ["FIREWORKS_MODEL"] = ""
        os.environ["OPENROUTER_API_KEY"] = ""
        self.assertEqual(main.Provider().name, "Local deterministic")

    def test_provider_preference_is_configuration_driven(self):
        os.environ["FIREWORKS_API_KEY"] = "fireworks-test-key"
        os.environ["FIREWORKS_MODEL"] = "fireworks-test-model"
        os.environ["OPENROUTER_API_KEY"] = "openrouter-test-key"
        os.environ["OPENROUTER_MODEL"] = "openrouter-test-model"
        os.environ["SECONDHELLO_PROVIDER"] = "openrouter"
        self.assertEqual(main.Provider().name, "OpenRouter")


if __name__ == "__main__":
    unittest.main()
