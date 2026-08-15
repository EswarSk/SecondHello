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

    def test_sentence_fragment_cannot_be_saved_as_person(self):
        result = self.capture("fragment", "looking for opportunities", "", "I am looking for opportunities and people who work on agentic AI.")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "valid_person_name_required")
        self.assertFalse(self.path.exists())

    def test_research_candidate_name_requires_cited_source_support(self):
        self.assertTrue(main.source_mentions_name("Mansour Saffar", {"url": "https://example.com/mansour-saffar", "title": "Profile", "quote": ""}))
        self.assertFalse(main.source_mentions_name("Mansour Saffar", {"url": "https://example.com/generic", "title": "Profile", "quote": "A professional profile"}))

    def test_capture_calls_extract_persist_and_follow_up_tools(self):
        result = self.capture("p1", "Alex", "alex@example.com", "I need an applied ML partner. I can offer climate domain expertise.")
        self.assertTrue(result["ok"])
        self.assertEqual([item["tool"] for item in result["trace"]], ["consent_gate", "extract_memory", "resolve_identity", "save_contact", "plan_public_research", "web_research", "verify_sources", "persist_memory", "prepare_follow_up"])
        self.assertEqual(main.BACKEND.load()["people"][0]["name"], "Alex")

    def test_every_conversation_becomes_a_grounded_follow_up_without_scripted_phrasing(self):
        transcript = "Hi, I am Priya. We talked about the product launch, her retail analytics work, and meeting again next week."
        result = self.capture("p1", "Priya", "", transcript)
        self.assertTrue(result["ok"])
        self.assertEqual(result["followUp"]["personName"], "Priya")
        self.assertIn("product launch", result["followUp"]["summary"])
        self.assertIn("product launch", result["followUp"]["connectionNote"])
        self.assertLessEqual(len(result["followUp"]["connectionNote"]), 300)
        self.assertEqual(main.BACKEND.load()["followUps"][0]["conversationID"], "conversation-p1")

    def test_first_name_is_saved_but_public_search_is_deferred(self):
        result = self.capture("p1", "Sam", "", "Hi, I am Sam. It was great meeting you today.")
        self.assertTrue(result["ok"])
        self.assertEqual(result["identityResolution"]["status"], "collecting")
        research_trace = next(item for item in result["trace"] if item["tool"] == "web_research")
        self.assertTrue(research_trace["skipped"])
        self.assertEqual(result["followUp"]["personName"], "Sam")

    def test_first_name_with_role_and_company_is_ready_for_identity_search(self):
        profile = main.local_profile("I'm Ray. I'm the head of marketing for Retail AI. We talked about agentic AI.", "conversation")
        resolution = main.identity_resolution({"name": "Ray"}, profile)
        self.assertEqual(profile["role"].lower(), "head of marketing")
        self.assertEqual(profile["company"], "Retail AI")
        self.assertEqual(resolution["status"], "ready")

    def test_incremental_snapshots_reuse_completed_identity_research(self):
        os.environ["OPENROUTER_API_KEY"] = "configured-for-test"
        os.environ["OPENROUTER_MODEL"] = "openrouter-test-model"
        os.environ["SECONDHELLO_PROVIDER"] = "openrouter"
        main.PROVIDER = main.Provider()
        parsed = {"needs": [], "offers": [], "topics": ["retail analytics"], "commitments": [], "role": "head of marketing", "company": "Retail AI"}
        with patch.object(main.PROVIDER, "json_completion", return_value=parsed), patch.object(main.PROVIDER, "public_research", return_value=({}, "OpenRouter Web Search")) as research:
            self.capture("p1", "Jim Sharf", "", "I'm Jim Sharf. I'm the head of marketing for Retail AI. We discussed retail analytics.")
            result = self.capture("p1", "Jim Sharf", "", "I'm Jim Sharf. I'm the head of marketing for Retail AI. We discussed retail analytics and agentic AI.")
        self.assertEqual(research.call_count, 1)
        plan_trace = next(item for item in result["trace"] if item["tool"] == "plan_public_research")
        self.assertTrue(plan_trace["cached"])

    def test_openrouter_research_requires_citations_before_enrichment(self):
        os.environ["OPENROUTER_API_KEY"] = "configured-for-test"
        os.environ["OPENROUTER_MODEL"] = "openrouter-test-model"
        os.environ["SECONDHELLO_PROVIDER"] = "openrouter"
        main.PROVIDER = main.Provider()
        main.PROVIDER.embedding_model = ""
        response = {"choices": [{"message": {
            "content": '{"matched":true,"confidence":0.91,"summary":"Climate software founder","roles":["Founder"],"offers":["climate domain expertise"],"sources":[{"title":"Profile","url":"https://example.com/alex","quote":"Alex builds climate software."}],"candidateConnections":[{"name":"Jordan","role":"ML architect","rationale":"Applied ML partner","supportedCapability":"applied ML partner","source":{"title":"Jordan profile","url":"https://example.com/jordan","quote":"Jordan designs applied ML systems."}}]}',
            "annotations": [{"type": "url_citation", "url_citation": {"url": "https://example.com/alex", "title": "Profile", "content": "Alex builds climate software."}}, {"type": "url_citation", "url_citation": {"url": "https://example.com/jordan", "title": "Jordan profile", "content": "Jordan designs applied ML systems."}}],
        }}]}
        with patch.object(main.PROVIDER, "json_completion", return_value=None), patch.object(main.PROVIDER, "_post", return_value=response) as post:
            result = self.capture("p1", "Alex Example", "", "I need an applied ML partner.")
        self.assertTrue(post.called)
        self.assertEqual(result["profile"]["publicOffers"], ["climate domain expertise"])
        self.assertEqual(result["profile"]["researchEvidence"][0]["sourceURL"], "https://example.com/alex")
        self.assertEqual(result["profile"]["publicCandidates"][0]["name"], "Jordan")
        self.assertEqual(result["followUp"]["personName"], "Alex Example")
        self.assertIn("verify_sources", [item["tool"] for item in result["trace"]])

    def test_provider_questions_do_not_become_networking_needs_or_offers(self):
        os.environ["OPENROUTER_API_KEY"] = "configured-for-test"
        os.environ["OPENROUTER_MODEL"] = "openrouter-test-model"
        os.environ["SECONDHELLO_PROVIDER"] = "openrouter"
        os.environ["SECONDHELLO_PUBLIC_RESEARCH"] = "0"
        main.PROVIDER = main.Provider()
        parsed = {
            "needs": ["Hear what you are working on", "What problem are you trying to solve?"],
            "offers": ["A five-minute chat", "Come say hi"],
            "topics": ["Hackathon projects"],
            "commitments": [],
        }
        with patch.object(main.PROVIDER, "json_completion", return_value=parsed), patch.object(main.PROVIDER, "public_research", return_value=({}, "disabled")):
            result = self.capture("p1", "Sam Example", "", "I am a founder. I would love to hear what you are building. What problem are you trying to solve?")
        self.assertEqual(result["profile"]["needs"], [])
        self.assertEqual(result["profile"]["offers"], [])
        self.assertEqual(result["profile"]["topics"], [])

    def test_semantic_match_has_no_named_demo_branch(self):
        self.capture("p1", "Alex", "alex@example.com", "I need an applied ML partner. I can offer climate domain expertise.")
        self.capture("p2", "Jordan", "jordan@example.com", "I can offer applied ML architecture. I need climate domain expertise.")
        result = main.run_workflow({"action": "match"})
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

    def test_scribe_token_uses_server_key_without_returning_it(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self): return b'{"token":"sutkn-test"}'

        os.environ["ELEVENLABS_API_KEY"] = "server-only-scribe-key"
        with patch.object(main.urllib.request, "urlopen", return_value=Response()) as urlopen:
            status, payload = main.elevenlabs_scribe_token()

        request = urlopen.call_args.args[0]
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True, "token": "sutkn-test", "modelId": "scribe_v2_realtime"})
        self.assertEqual(request.get_header("Xi-api-key"), "server-only-scribe-key")
        self.assertNotIn("server-only-scribe-key", str(payload))

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
