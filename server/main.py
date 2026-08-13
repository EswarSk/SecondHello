#!/usr/bin/env python3
"""Second Hello's consent-first LangGraph service.

Configured services are used for real calls. With no credentials, the exact same
workflow runs with local extraction, embeddings, persistence, and drafting so a
demo never depends on venue Wi-Fi.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict
from uuid import uuid4

try:
    from langgraph.graph import END, START, StateGraph
except ImportError:
    StateGraph = None
    START = END = None


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_project_env(path: Path = PROJECT_ROOT / ".env") -> None:
    """Load local configuration without overwriting exported environment values."""
    if not path.is_file():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(path, override=False)
        return
    except ImportError:
        pass

    # Keep .env support available in zero-dependency Demo Mode. python-dotenv,
    # when installed from requirements.txt, handles the broader dotenv syntax.
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


load_project_env()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def mongodb_uri() -> str:
    """Return an explicit URI or safely construct one from split Atlas settings."""
    if configured_uri := env("MONGODB_URI"):
        return configured_uri
    username = env("MONGODB_USERNAME")
    password = env("MONGODB_PASSWORD")
    cluster_host = env("MONGODB_CLUSTER_HOST")
    if not (username and password and cluster_host):
        return ""
    database = env("MONGODB_DATABASE", "secondhello")
    options = env("MONGODB_OPTIONS", "retryWrites=true&w=majority&appName=SecondHello")
    encoded_user = urllib.parse.quote(username, safe="")
    encoded_password = urllib.parse.quote(password, safe="")
    encoded_database = urllib.parse.quote(database, safe="")
    return f"mongodb+srv://{encoded_user}:{encoded_password}@{cluster_host}/{encoded_database}?{options}"


class WorkflowState(TypedDict, total=False):
    action: str
    person: dict[str, Any]
    conversation: dict[str, Any]
    introduction: dict[str, Any]
    action_receipt: dict[str, Any]
    memory: dict[str, Any]
    profile: dict[str, Any]
    opportunities: list[dict[str, Any]]
    draft: dict[str, str]
    route: str
    ok: bool
    reason: str
    trace: list[dict[str, Any]]


def trace(state: WorkflowState, tool: str, detail: str, mode: str) -> list[dict[str, Any]]:
    return state.get("trace", []) + [{"id": str(uuid4()), "tool": tool, "detail": detail, "mode": mode, "completedAt": utc_now()}]


class Provider:
    """Small OpenAI-compatible adapter for Fireworks and OpenRouter."""

    def __init__(self) -> None:
        fireworks_ready = bool(env("FIREWORKS_API_KEY") and env("FIREWORKS_MODEL"))
        openrouter_ready = bool(env("OPENROUTER_API_KEY") and env("OPENROUTER_MODEL"))
        preferred = env("SECONDHELLO_PROVIDER").lower()
        if preferred in {"fireworks", "openrouter"}:
            selected = preferred
        elif fireworks_ready:
            selected = "fireworks"
        elif openrouter_ready:
            selected = "openrouter"
        else:
            selected = "local"

        if selected == "fireworks" and fireworks_ready:
            self.name = "Fireworks"
            self.key = env("FIREWORKS_API_KEY")
            self.model = env("FIREWORKS_MODEL")
            self.chat_url = env("FIREWORKS_CHAT_URL", "https://api.fireworks.ai/inference/v1/chat/completions")
            self.embedding_url = env("FIREWORKS_EMBEDDING_URL", "https://api.fireworks.ai/inference/v1/embeddings")
            self.embedding_model = env("FIREWORKS_EMBEDDING_MODEL")
        elif selected == "openrouter" and openrouter_ready:
            self.name = "OpenRouter"
            self.key = env("OPENROUTER_API_KEY")
            self.model = env("OPENROUTER_MODEL")
            self.chat_url = env("OPENROUTER_CHAT_URL", "https://openrouter.ai/api/v1/chat/completions")
            self.embedding_url = env("OPENROUTER_EMBEDDING_URL", "https://openrouter.ai/api/v1/embeddings")
            self.embedding_model = env("OPENROUTER_EMBEDDING_MODEL")
        else:
            self.name = "Local deterministic"
            self.key = self.model = self.chat_url = self.embedding_url = self.embedding_model = ""

    @property
    def configured(self) -> bool:
        return bool(self.key and self.model)

    def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json", "X-Title": "Second Hello"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=float(env("PROVIDER_TIMEOUT_SECONDS", "12"))) as response:
            return json.loads(response.read())

    def json_completion(self, system: str, user: str) -> dict[str, Any] | None:
        if not self.configured:
            return None
        try:
            result = self._post(self.chat_url, {
                "model": self.model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            })
            content = result["choices"][0]["message"]["content"]
            if isinstance(content, str) and content.startswith("```"):
                content = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.I).strip()
            return json.loads(content)
        except (urllib.error.URLError, TimeoutError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def embedding(self, text: str, purpose: str = "document") -> tuple[list[float], str]:
        if self.configured and self.embedding_model:
            try:
                embedding_input = text
                if self.name == "Fireworks" and "qwen3-embedding" in self.embedding_model.lower() and purpose == "query":
                    embedding_input = (
                        "Instruct: Find a person whose explicitly stated offer can satisfy this networking need\n"
                        f"Query: {text}"
                    )
                payload: dict[str, Any] = {"model": self.embedding_model, "input": [embedding_input]}
                dimension_name = "FIREWORKS_EMBEDDING_DIMENSIONS" if self.name == "Fireworks" else "OPENROUTER_EMBEDDING_DIMENSIONS"
                if dimensions := env(dimension_name):
                    payload["dimensions"] = int(dimensions)
                result = self._post(self.embedding_url, payload)
                return normalize_vector([float(x) for x in result["data"][0]["embedding"]]), self.name
            except (urllib.error.URLError, TimeoutError, KeyError, IndexError, TypeError, ValueError):
                pass
        fallback_dimension_name = "FIREWORKS_EMBEDDING_DIMENSIONS" if self.name == "Fireworks" else "OPENROUTER_EMBEDDING_DIMENSIONS"
        fallback_dimensions = int(env(fallback_dimension_name) or "96")
        return deterministic_embedding(text, fallback_dimensions), "Local deterministic"


PROVIDER = Provider()


def tokens(value: str) -> list[str]:
    stop = {"the", "and", "for", "with", "that", "this", "from", "into", "our", "can", "looking", "need", "offer"}
    return [word for word in re.findall(r"[a-z0-9]+", value.lower()) if len(word) > 2 and word not in stop]


def deterministic_embedding(text: str, dimensions: int = 96) -> list[float]:
    vector = [0.0] * dimensions
    for token in tokens(text):
        digest = hashlib.sha256(token.encode()).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        vector[index] += -1.0 if digest[4] & 1 else 1.0
    length = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / length for value in vector]


def normalize_vector(vector: list[float]) -> list[float]:
    length = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / length for value in vector]


def cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        return 0.0
    left_length = math.sqrt(sum(value * value for value in left)) or 1.0
    right_length = math.sqrt(sum(value * value for value in right)) or 1.0
    return sum(a * b for a, b in zip(left, right)) / (left_length * right_length)


def sentences(text: str) -> list[str]:
    return [item.strip() for item in re.split(r"(?<=[.!?])\s+", text.strip()) if item.strip()]


def local_profile(transcript: str, conversation_id: str) -> dict[str, Any]:
    lines = sentences(transcript)
    rules = {
        "needs": (r"\b(?:need|needs|looking for|seeking)\b\s*(.*)",),
        "offers": (r"\b(?:can offer|offer|offers|can help with|i build|i work on)\b\s*(.*)",),
        "topics": (r"\b(?:interested in|care about|focused on|passionate about)\b\s*(.*)",),
        "commitments": (r"\b(?:i will|i'll|will send|will share|will introduce|follow up)\b.*",),
    }
    result: dict[str, Any] = {key: [] for key in rules}
    for line in lines:
        for key, patterns in rules.items():
            for pattern in patterns:
                found = re.search(pattern, line, flags=re.I)
                if found:
                    value = found.group(1) if found.lastindex else found.group(0)
                    value = value.strip(" .,:;-")
                    if value and value not in result[key]:
                        result[key].append(value)
                    break
    result["evidence"] = [{"id": str(uuid4()), "quote": line, "conversationID": conversation_id, "capturedAt": utc_now()} for line in lines]
    return result


def extract_profile(transcript: str, conversation_id: str) -> tuple[dict[str, Any], str]:
    schema_prompt = (
        "Extract only explicitly stated networking memory. Return JSON with arrays named needs, offers, topics, commitments. "
        "Do not infer sensitive traits or facts. Preserve concise wording from the transcript."
    )
    parsed = PROVIDER.json_completion(schema_prompt, transcript)
    if parsed is None:
        return local_profile(transcript, conversation_id), "Local deterministic"
    profile = {}
    for key in ("needs", "offers", "topics", "commitments"):
        values = parsed.get(key, [])
        profile[key] = [str(value).strip() for value in values if str(value).strip()] if isinstance(values, list) else []
    profile["evidence"] = [{"id": str(uuid4()), "quote": line, "conversationID": conversation_id, "capturedAt": utc_now()} for line in sentences(transcript)]
    return profile, PROVIDER.name


class MemoryBackend:
    """Atlas-backed memory and action audit with atomic local JSON fallback."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.path = Path(env("SECONDHELLO_MEMORY_FILE") or (Path.home() / ".secondhello" / "memory.json"))
        self.mode = "Local JSON"
        self.client = self.database = None
        uri = mongodb_uri()
        if uri:
            try:
                from pymongo import MongoClient
                self.client = MongoClient(uri, serverSelectionTimeoutMS=int(env("MONGODB_TIMEOUT_MS", "2500")))
                self.client.admin.command("ping")
                self.database = self.client[env("MONGODB_DATABASE", "secondhello")]
                self.mode = "MongoDB Atlas"
            except Exception as error:
                self.client = self.database = None
                self.startup_warning = f"Atlas unavailable ({type(error).__name__}); using local JSON"

    def _empty(self) -> dict[str, Any]:
        return {"schemaVersion": 2, "people": [], "conversations": [], "actions": []}

    def load(self) -> dict[str, Any]:
        if self.database is not None:
            return {
                "schemaVersion": 2,
                "people": list(self.database.people.find({}, {"_id": False})),
                "conversations": list(self.database.conversations.find({}, {"_id": False, "embedding": False})),
                "actions": list(self.database.actions.find({}, {"_id": False})),
            }
        with self.lock:
            if not self.path.exists():
                return self._empty()
            data = json.loads(self.path.read_text())
            data.setdefault("actions", [])
            return data

    def _write_local(self, memory: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(memory, indent=2, sort_keys=True))
        temporary.replace(self.path)

    def persist_capture(self, person: dict[str, Any], conversation: dict[str, Any]) -> None:
        if self.database is not None:
            self.database.people.replace_one({"id": person["id"]}, person, upsert=True)
            self.database.conversations.replace_one({"id": conversation["id"]}, conversation, upsert=True)
            self.database.memory_items.delete_many({"conversationID": conversation["id"]})
            items = []
            evidence = conversation.get("profile", {}).get("evidence", [])
            for kind in ("needs", "offers", "topics", "commitments"):
                for text in conversation.get("profile", {}).get(kind, []):
                    vector, embedding_mode = PROVIDER.embedding(text, "document")
                    quote = next((e.get("quote", "") for e in evidence if text.lower() in e.get("quote", "").lower()), text)
                    items.append({"personID": person["id"], "personName": person["name"], "conversationID": conversation["id"], "kind": kind[:-1] if kind.endswith("s") else kind, "text": text, "quote": quote, "embedding": vector, "embeddingMode": embedding_mode, "capturedAt": conversation["timestamp"]})
            if items:
                self.database.memory_items.insert_many(items)
            return
        with self.lock:
            memory = self.load_unlocked()
            memory["people"] = [value for value in memory["people"] if value.get("id") != person["id"]] + [person]
            memory["conversations"] = [value for value in memory["conversations"] if value.get("id") != conversation["id"]] + [conversation]
            self._write_local(memory)

    def load_unlocked(self) -> dict[str, Any]:
        if not self.path.exists(): return self._empty()
        data = json.loads(self.path.read_text()); data.setdefault("actions", []); return data

    def record_action(self, receipt: dict[str, Any]) -> None:
        if self.database is not None:
            self.database.actions.replace_one({"id": receipt["id"]}, receipt, upsert=True); return
        with self.lock:
            memory = self.load_unlocked(); memory["actions"].append(receipt); self._write_local(memory)

    def vector_offers(self, query: list[float], exclude_person: str) -> list[dict[str, Any]]:
        if self.database is None or not env("MONGODB_VECTOR_INDEX"):
            return []
        try:
            pipeline = [{"$vectorSearch": {"index": env("MONGODB_VECTOR_INDEX"), "path": "embedding", "queryVector": query, "numCandidates": 100, "limit": 10}}, {"$match": {"kind": "offer", "personID": {"$ne": exclude_person}}}, {"$set": {"score": {"$meta": "vectorSearchScore"}}}, {"$project": {"_id": 0, "embedding": 0}}]
            return list(self.database.memory_items.aggregate(pipeline))
        except Exception:
            return []


BACKEND = MemoryBackend()


def validate_and_route(state: WorkflowState) -> WorkflowState:
    action = state.get("action", "")
    routes = {"capture": "extract", "match": "match", "draft": "draft", "record_action": "record_action"}
    if action not in routes:
        return {"route": "end", "ok": False, "reason": "unsupported_action", "trace": trace(state, "consent_gate", "Rejected an unsupported action", "policy")}
    if action == "capture":
        conversation = state.get("conversation", {})
        if conversation.get("consented") is not True:
            return {"route": "end", "ok": False, "reason": "explicit_consent_required", "trace": trace(state, "consent_gate", "Blocked capture before extraction or storage", "policy")}
        if not str(conversation.get("transcript", "")).strip() or not state.get("person", {}).get("name"):
            return {"route": "end", "ok": False, "reason": "person_and_transcript_required", "trace": trace(state, "consent_gate", "Rejected incomplete capture", "policy")}
    return {"route": routes[action], "ok": True, "trace": trace(state, "consent_gate", "Permission and input checks passed", "policy")}


def extract_tool(state: WorkflowState) -> WorkflowState:
    conversation = state["conversation"]
    profile, mode = extract_profile(conversation["transcript"], conversation["id"])
    return {"profile": profile, "route": "persist", "trace": trace(state, "extract_memory", f"Extracted {sum(len(profile[key]) for key in ('needs', 'offers', 'topics', 'commitments'))} explicit memories", mode)}


def persist_tool(state: WorkflowState) -> WorkflowState:
    conversation = dict(state["conversation"]); conversation["profile"] = state["profile"]
    BACKEND.persist_capture(state["person"], conversation)
    return {"conversation": conversation, "route": "match", "trace": trace(state, "persist_memory", "Stored consent receipt, evidence, and structured memory", BACKEND.mode)}


def profile_map(memory: dict[str, Any]) -> dict[str, dict[str, Any]]:
    profiles: dict[str, dict[str, Any]] = {}
    for conversation in memory.get("conversations", []):
        current = profiles.setdefault(conversation.get("personID", ""), {"needs": [], "offers": [], "evidence": []})
        profile = conversation.get("profile", {})
        for key in ("needs", "offers"):
            current[key].extend(value for value in profile.get(key, []) if value not in current[key])
        current["evidence"].extend(profile.get("evidence", []))
    return profiles


def evidence_for(profile: dict[str, Any], text: str) -> dict[str, Any]:
    return next((item for item in profile.get("evidence", []) if text.lower() in item.get("quote", "").lower()), {"id": str(uuid4()), "quote": text, "conversationID": "", "capturedAt": utc_now()})


def match_tool(state: WorkflowState) -> WorkflowState:
    memory = BACKEND.load()
    people = {person["id"]: person for person in memory.get("people", [])}
    profiles = profile_map(memory)
    opportunities: list[dict[str, Any]] = []
    search_mode = "Local semantic"
    minimum = float(env("SECONDHELLO_MATCH_THRESHOLD", "0.18"))
    for recipient_id, recipient_profile in profiles.items():
        for need in recipient_profile.get("needs", []):
            query, embedding_mode = PROVIDER.embedding(need, "query")
            atlas = BACKEND.vector_offers(query, recipient_id)
            if atlas:
                candidates = [(float(item.get("score", 0)), item["personID"], item["text"], item.get("quote", item["text"])) for item in atlas]
                search_mode = "Atlas Vector Search"
            else:
                candidates = []
                for connector_id, connector_profile in profiles.items():
                    if connector_id == recipient_id: continue
                    for offer in connector_profile.get("offers", []):
                        offer_vector, _ = PROVIDER.embedding(offer, "document")
                        candidates.append((cosine(query, offer_vector), connector_id, offer, evidence_for(connector_profile, offer)["quote"]))
            for score, connector_id, offer, offer_quote in sorted(candidates, reverse=True)[:1]:
                if score < minimum or connector_id not in people or recipient_id not in people: continue
                opportunities.append({
                    "id": str(uuid4()), "recipientID": recipient_id, "recipientName": people[recipient_id]["name"], "recipientEmail": people[recipient_id].get("email"),
                    "connectorID": connector_id, "connectorName": people[connector_id]["name"], "connectorEmail": people[connector_id].get("email"),
                    "need": need, "offer": offer, "score": round(score, 3), "needEvidence": evidence_for(recipient_profile, need),
                    "offerEvidence": {"id": str(uuid4()), "quote": offer_quote, "conversationID": "", "capturedAt": utc_now()}, "searchMode": search_mode,
                })
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for opportunity in opportunities:
        key = (opportunity["recipientID"], opportunity["connectorID"])
        if key not in unique or opportunity["score"] > unique[key]["score"]: unique[key] = opportunity
    values = sorted(unique.values(), key=lambda item: item["score"], reverse=True)
    return {"memory": memory, "opportunities": values, "route": "end", "trace": trace(state, "find_introductions", f"Compared consented needs and offers; found {len(values)} opportunity(s)", search_mode)}


def draft_tool(state: WorkflowState) -> WorkflowState:
    idea = state.get("introduction", {})
    recipient = idea.get("recipientName", "the first person")
    connector = idea.get("connectorName", "the second person")
    local = {"to": idea.get("recipientEmail") or "", "cc": idea.get("connectorEmail") or "", "subject": f"Intro: {recipient} × {connector}", "body": f"Hi {recipient} and {connector},\n\nYou both explicitly said you were open to relevant introductions. {recipient} is looking for {idea.get('need', 'support')}; {connector} can offer {idea.get('offer', 'relevant experience')}.\n\nWould you like to connect? I’ll leave it to you both from here.\n"}
    prompt = "Return JSON with subject and body for a warm, concise double-opt-in introduction. Do not invent facts. Do not claim a message was sent."
    generated = PROVIDER.json_completion(prompt, json.dumps({"recipient": recipient, "connector": connector, "need": idea.get("need"), "offer": idea.get("offer")}))
    mode = "Local deterministic"
    if generated and generated.get("subject") and generated.get("body"):
        local["subject"] = str(generated["subject"]); local["body"] = str(generated["body"]); mode = PROVIDER.name
    return {"draft": local, "route": "end", "trace": trace(state, "compose_introduction", "Created an editable draft; nothing was sent", mode)}


def record_action_tool(state: WorkflowState) -> WorkflowState:
    receipt = state.get("action_receipt", {})
    receipt.setdefault("id", str(uuid4())); receipt.setdefault("createdAt", utc_now())
    BACKEND.record_action(receipt)
    return {"action_receipt": receipt, "route": "end", "trace": trace(state, "record_action", "Recorded the user-approved handoff to the default mail app", BACKEND.mode)}


def response(state: WorkflowState) -> dict[str, Any]:
    return {key: state[key] for key in ("ok", "reason", "profile", "conversation", "opportunities", "draft", "action_receipt", "trace") if key in state}


def run_local(initial: WorkflowState) -> WorkflowState:
    state = dict(initial); state.update(validate_and_route(state))
    while state.get("route") != "end":
        node = {"extract": extract_tool, "persist": persist_tool, "match": match_tool, "draft": draft_tool, "record_action": record_action_tool}[state["route"]]
        state.update(node(state))
    return state


def build_graph():
    if StateGraph is None: return None
    graph = StateGraph(WorkflowState)
    graph.add_node("guard", validate_and_route); graph.add_node("extract", extract_tool); graph.add_node("persist", persist_tool); graph.add_node("match", match_tool); graph.add_node("draft", draft_tool); graph.add_node("record_action", record_action_tool)
    graph.add_edge(START, "guard")
    graph.add_conditional_edges("guard", lambda state: state.get("route", "end"), {"extract": "extract", "match": "match", "draft": "draft", "record_action": "record_action", "end": END})
    graph.add_edge("extract", "persist"); graph.add_edge("persist", "match"); graph.add_edge("match", END); graph.add_edge("draft", END); graph.add_edge("record_action", END)
    return graph.compile()


GRAPH = build_graph()


def run_workflow(payload: dict[str, Any]) -> dict[str, Any]:
    initial: WorkflowState = {**payload, "trace": [], "ok": True}
    final = GRAPH.invoke(initial) if GRAPH is not None else run_local(initial)
    return response(final)


def health() -> dict[str, Any]:
    return {
        "ok": True,
        "workflow": "LangGraph" if GRAPH is not None else "Local graph runtime",
        "storage": BACKEND.mode,
        "provider": PROVIDER.name,
        "vectorSearch": bool(BACKEND.database is not None and env("MONGODB_VECTOR_INDEX")),
        "voiceAgentConfigured": bool(env("ELEVENLABS_API_KEY") and env("ELEVENLABS_AGENT_ID")),
        "safeFallback": True,
    }


def elevenlabs_signed_url() -> tuple[int, dict[str, Any]]:
    """Create a short-lived agent URL without exposing the ElevenLabs API key."""
    api_key = env("ELEVENLABS_API_KEY")
    agent_id = env("ELEVENLABS_AGENT_ID")
    if not (api_key and agent_id):
        return 503, {"ok": False, "reason": "elevenlabs_api_key_and_agent_id_required"}
    query = urllib.parse.urlencode({"agent_id": agent_id, "include_conversation_id": "true"})
    request = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/convai/conversation/get-signed-url?{query}",
        headers={"xi-api-key": api_key, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=float(env("PROVIDER_TIMEOUT_SECONDS", "12"))) as response:
            payload = json.loads(response.read())
        return 200, {"ok": True, "signedUrl": payload["signed_url"]}
    except (urllib.error.URLError, TimeoutError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return 502, {"ok": False, "reason": "elevenlabs_signed_url_unavailable"}


def send_json(handler: Any, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, default=str).encode()
    handler.send_response(status); handler.send_header("Content-Type", "application/json"); handler.send_header("Content-Length", str(len(body))); handler.end_headers(); handler.wfile.write(body)


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: Any) -> None: pass
        def do_GET(self) -> None:
            if self.path == "/health": send_json(self, 200, health())
            elif self.path == "/memory": send_json(self, 200, BACKEND.load())
            elif self.path == "/elevenlabs/signed-url":
                status, payload = elevenlabs_signed_url(); send_json(self, status, payload)
            else: send_json(self, 404, {"ok": False, "reason": "not_found"})
        def do_POST(self) -> None:
            if self.path != "/workflow": send_json(self, 404, {"ok": False, "reason": "not_found"}); return
            try:
                size = int(self.headers.get("Content-Length", 0)); payload = json.loads(self.rfile.read(size)); send_json(self, 200, run_workflow(payload))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                send_json(self, 400, {"ok": False, "reason": type(error).__name__})
    print(f"Second Hello · {host}:{port} · {health()}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    serve(env("SECONDHELLO_HOST", "127.0.0.1"), int(env("SECONDHELLO_PORT", "8765")))
