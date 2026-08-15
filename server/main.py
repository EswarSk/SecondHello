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
from uuid import NAMESPACE_URL, uuid4, uuid5

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
    raw_options = env("MONGODB_OPTIONS", "")
    option_parts = [part for part in raw_options.split("&") if "=" in part]
    options = "&".join(option_parts) or "retryWrites=true&w=majority&appName=SecondHello"
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
    identityResolution: dict[str, Any]
    research_query: str
    research: dict[str, Any]
    followUp: dict[str, Any]
    opportunities: list[dict[str, Any]]
    draft: dict[str, str]
    route: str
    ok: bool
    reason: str
    trace: list[dict[str, Any]]


def trace(state: WorkflowState, tool: str, detail: str, mode: str, **metadata: Any) -> list[dict[str, Any]]:
    entry = {"id": str(uuid4()), "tool": tool, "detail": detail, "mode": mode, "completedAt": utc_now()}
    entry.update({key: value for key, value in metadata.items() if value is not None})
    return state.get("trace", []) + [entry]


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

    def _post(self, url: str, payload: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json", "X-Title": "Second Hello"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout or float(env("PROVIDER_TIMEOUT_SECONDS", "12"))) as response:
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

    def public_research(self, query: str) -> tuple[dict[str, Any], str]:
        """Research public professional facts with citations; never guess identity."""
        if self.name != "OpenRouter" or not self.configured or env("SECONDHELLO_PUBLIC_RESEARCH", "1") == "0":
            return {}, "Public research unavailable"
        system = (
            "You are an identity-safe professional networking researcher. Use web search. "
            "Resolve the person only when the name and supplied professional context agree. "
            "Do not collect sensitive traits, personal addresses, phone numbers, family details, or private data. "
            "Return strict JSON with: matched (boolean), confidence (0..1), summary (string), "
            "roles (array of concise public professional roles), offers (array of capabilities this person could plausibly offer), "
            "sources (array of {title,url,quote}), and candidateConnections (array of at most 3 objects with "
            "name, role, rationale, supportedCapability, and source {title,url,quote}). Candidate connections must directly address "
            "an explicit need in the supplied context and be public professionals discoverable in authoritative sources. "
            "Every role, offer, and candidate must be supported by a source. Do not return contact information. "
            "If identity is ambiguous, set matched=false and return no roles or offers."
        )
        try:
            result = self._post(self.chat_url, {
                "model": self.model,
                "temperature": 0,
                "tools": [{"type": "openrouter:web_search", "parameters": {
                    "engine": env("OPENROUTER_SEARCH_ENGINE", "auto"),
                    "max_results": int(env("OPENROUTER_SEARCH_RESULTS", "6")),
                    "max_total_results": int(env("OPENROUTER_SEARCH_TOTAL_RESULTS", "10")),
                    "max_uses": int(env("OPENROUTER_SEARCH_MAX_USES", "3")),
                    "search_context_size": env("OPENROUTER_SEARCH_CONTEXT_SIZE", "medium"),
                }}],
                "max_tool_calls": int(env("OPENROUTER_SEARCH_MAX_USES", "3")),
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": query}],
            }, timeout=float(env("PUBLIC_RESEARCH_TIMEOUT_SECONDS", "15")))
            message = result["choices"][0]["message"]
            content = message["content"]
            if isinstance(content, str) and content.startswith("```"):
                content = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.I).strip()
            parsed = json.loads(content)
            citations: dict[str, dict[str, str]] = {}
            for annotation in message.get("annotations", []):
                citation = annotation.get("url_citation", annotation)
                url = str(citation.get("url", "")).strip()
                if url.startswith(("https://", "http://")):
                    citations[url] = {"url": url, "title": str(citation.get("title", "Public source")), "quote": str(citation.get("content", ""))[:500]}
            sources = []
            for source in parsed.get("sources", []) if isinstance(parsed.get("sources"), list) else []:
                if not isinstance(source, dict): continue
                url = str(source.get("url", "")).strip()
                if not url.startswith(("https://", "http://")): continue
                cited = citations.get(url, {})
                sources.append({"url": url, "title": str(source.get("title") or cited.get("title") or "Public source"), "quote": str(source.get("quote") or cited.get("quote") or "")[:500]})
            for url, citation in citations.items():
                if not any(item["url"] == url for item in sources): sources.append(citation)
            parsed["sources"] = sources
            candidates = []
            for candidate in parsed.get("candidateConnections", []) if isinstance(parsed.get("candidateConnections"), list) else []:
                if not isinstance(candidate, dict): continue
                source = candidate.get("source", {}) if isinstance(candidate.get("source"), dict) else {}
                url = str(source.get("url", "")).strip()
                if not url.startswith(("https://", "http://")): continue
                cited = citations.get(url, {})
                name = str(candidate.get("name", "")).strip()
                capability = str(candidate.get("supportedCapability", "")).strip()
                supporting_source = {**cited, **source}
                if not (valid_person_name(name) and capability and source_mentions_name(name, supporting_source)): continue
                candidates.append({
                    "name": name, "role": str(candidate.get("role", "")).strip(),
                    "rationale": str(candidate.get("rationale", "")).strip(), "supportedCapability": capability,
                    "source": {"url": url, "title": str(source.get("title") or cited.get("title") or "Public source"), "quote": str(source.get("quote") or cited.get("quote") or "")[:500]},
                })
            parsed["candidateConnections"] = candidates[:3]
            return parsed, "OpenRouter Web Search"
        except (urllib.error.URLError, TimeoutError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            return {}, "Public research unavailable"

    def embeddings(self, texts: list[str], purpose: str = "document") -> tuple[list[list[float]], str]:
        if not texts:
            return [], self.name if self.configured and self.embedding_model else "Local deterministic"
        if self.configured and self.embedding_model:
            try:
                embedding_inputs = list(texts)
                if self.name == "Fireworks" and "qwen3-embedding" in self.embedding_model.lower() and purpose == "query":
                    embedding_inputs = [(
                        "Instruct: Find a person whose explicitly stated offer can satisfy this networking need\n"
                        f"Query: {text}"
                    ) for text in texts]
                payload: dict[str, Any] = {"model": self.embedding_model, "input": embedding_inputs}
                dimension_name = "FIREWORKS_EMBEDDING_DIMENSIONS" if self.name == "Fireworks" else "OPENROUTER_EMBEDDING_DIMENSIONS"
                if dimensions := env(dimension_name):
                    payload["dimensions"] = int(dimensions)
                result = self._post(self.embedding_url, payload)
                ordered = sorted(result["data"], key=lambda item: int(item.get("index", 0)))
                vectors = [normalize_vector([float(x) for x in item["embedding"]]) for item in ordered]
                if len(vectors) == len(texts):
                    return vectors, self.name
            except (urllib.error.URLError, TimeoutError, KeyError, IndexError, TypeError, ValueError):
                pass
        fallback_dimension_name = "FIREWORKS_EMBEDDING_DIMENSIONS" if self.name == "Fireworks" else "OPENROUTER_EMBEDDING_DIMENSIONS"
        fallback_dimensions = int(env(fallback_dimension_name) or "96")
        return [deterministic_embedding(text, fallback_dimensions) for text in texts], "Local deterministic"

    def embedding(self, text: str, purpose: str = "document") -> tuple[list[float], str]:
        vectors, mode = self.embeddings([text], purpose)
        return vectors[0], mode


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


PERSON_NAME_STOPWORDS = {
    "a", "about", "an", "and", "at", "for", "from", "here", "i", "in", "is", "it",
    "just", "looking", "my", "of", "on", "or", "people", "seeking", "that", "the", "there",
    "this", "to", "trying", "who", "with", "working",
}


def valid_person_name(value: Any) -> bool:
    """Accept only a short, name-shaped value—not a sentence fragment."""
    name = " ".join(str(value or "").strip().split())
    if not name or len(name) > 100:
        return False
    if not re.fullmatch(r"[^\W\d_][\w'’-]*(?:\s+[^\W\d_][\w'’-]*){0,3}", name, flags=re.UNICODE):
        return False
    words = name.split()
    return not any(word.casefold() in PERSON_NAME_STOPWORDS for word in words)


def source_mentions_name(name: str, source: dict[str, Any]) -> bool:
    """Require the cited title, quote, or URL to support a research candidate's name."""
    tokens = [token.casefold() for token in re.findall(r"[^\W\d_]+", name, flags=re.UNICODE)]
    if not tokens:
        return False
    source_text = " ".join(str(source.get(key, "")) for key in ("title", "url", "quote", "content")).casefold()
    source_tokens = set(re.findall(r"[^\W\d_]+", source_text, flags=re.UNICODE))
    compact_source = "".join(source_tokens)
    return all(token in source_tokens for token in tokens) or "".join(tokens) in compact_source


def sanitize_memory(memory: dict[str, Any]) -> dict[str, Any]:
    """Keep untrusted historical records from entering matching or the UI."""
    people = [person for person in memory.get("people", []) if isinstance(person, dict) and valid_person_name(person.get("name"))]
    person_ids = {person.get("id") for person in people}
    conversations = []
    for original in memory.get("conversations", []):
        if not isinstance(original, dict) or original.get("personID") not in person_ids:
            continue
        conversation = dict(original)
        profile = dict(conversation.get("profile", {}))
        profile["publicCandidates"] = [
            candidate for candidate in profile.get("publicCandidates", [])
            if isinstance(candidate, dict)
            and valid_person_name(candidate.get("name"))
            and isinstance(candidate.get("source"), dict)
            and source_mentions_name(str(candidate.get("name")), candidate["source"])
        ]
        conversation["profile"] = profile
        conversations.append(conversation)
    follow_ups = [
        item for item in memory.get("followUps", [])
        if isinstance(item, dict)
        and item.get("personID") in person_ids
        and valid_person_name(item.get("personName"))
    ]
    return {
        **memory,
        "schemaVersion": 3,
        "people": people,
        "conversations": conversations,
        "followUps": follow_ups,
        "actions": memory.get("actions", []),
    }


def local_profile(transcript: str, conversation_id: str) -> dict[str, Any]:
    lines = sentences(transcript)
    rules = {
        "needs": (r"\b(?:need|needs|looking for|searching for|seeking|trying to find|want|would like)\b\s*(.*)",),
        "offers": (r"\b(?:can offer|offer|offers|can help with|happy to|provide|can introduce|can connect|i build|i work on)\b\s*(.*)",),
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
                    # Speech-to-text often places two explicit facts in one
                    # sentence: "I need X and I can offer Y". Keep each fact
                    # bounded so the matcher never treats the whole sentence
                    # as one need or offer.
                    value = re.split(r"\s+(?:,\s*)?(?:and\s+)?i(?:'m| am| can| will| work| do| need| want)\b", value, maxsplit=1, flags=re.I)[0]
                    value = value.strip(" .,:;-")
                    if value and value not in result[key]:
                        result[key].append(value)
                    break
    result["evidence"] = [{"id": str(uuid4()), "quote": line, "conversationID": conversation_id, "capturedAt": utc_now()} for line in lines]
    result.update(explicit_identity_details(transcript))
    return result


def explicit_identity_details(transcript: str) -> dict[str, str]:
    """Extract only role/company phrases explicitly spoken in the conversation."""
    role = company = ""
    for line in sentences(transcript):
        normalized = " ".join(line.split())
        role_match = re.search(
            r"\b(?:i am|i['’]m)\s+(?:(?:the|a|an|one of the)\s+)?(.{2,80}?)\s+(?:at|for|with)\s+(?:a company called\s+)?([^,.!?;]{2,80})",
            normalized,
            flags=re.I,
        )
        if role_match:
            role_candidate = role_match.group(1).strip(" .,-")
            company_candidate = re.split(r"\s+(?:and|where|who|that)\b", role_match.group(2), maxsplit=1, flags=re.I)[0].strip(" .,-")
            if role_candidate and len(role_candidate.split()) <= 10:
                role = role or role_candidate
            if company_candidate and len(company_candidate.split()) <= 8:
                company = company or company_candidate
        company_match = re.search(
            r"\b(?:i\s+)?work(?:ing)?\s+(?:at|for|with)\s+(?:a company called\s+)?([^,.!?;]{2,80})",
            normalized,
            flags=re.I,
        )
        if company_match:
            company_candidate = re.split(r"\s+(?:and|where|who|that)\b", company_match.group(1), maxsplit=1, flags=re.I)[0].strip(" .,-")
            if company_candidate and len(company_candidate.split()) <= 8:
                company = company or company_candidate
    return {"role": role, "company": company}


def grounded_memory_value(value: Any, transcript: str, cue_pattern: str | None = None) -> bool:
    """Accept provider extraction only when a cited transcript sentence supports it."""
    candidate_tokens = set(tokens(str(value)))
    if not candidate_tokens:
        return False
    minimum_overlap = max(1, min(4, (len(candidate_tokens) + 1) // 2))
    for line in sentences(transcript):
        if cue_pattern and not re.search(cue_pattern, line, flags=re.I):
            continue
        overlap = candidate_tokens.intersection(tokens(line))
        if len(overlap) >= minimum_overlap:
            return True
    return False


NEED_CUE = r"\b(?:need|needs|looking for|searching for|seeking|trying to find|want|would like)\b"
OFFER_CUE = r"\b(?:can offer|offer|offers|can help with|happy to|provide|can introduce|can connect|i build|i work on)\b"
COMMITMENT_CUE = r"\b(?:i will|i'll|will send|will share|will introduce|follow up)\b"


def extract_profile(transcript: str, conversation_id: str) -> tuple[dict[str, Any], str]:
    schema_prompt = (
        "Extract only explicitly stated networking memory. Return JSON with arrays named needs, offers, topics, commitments "
        "and strings named role and company. "
        "Do not infer sensitive traits or facts. Preserve concise wording from the transcript."
    )
    parsed = PROVIDER.json_completion(schema_prompt, transcript)
    if parsed is None:
        return local_profile(transcript, conversation_id), "Local deterministic"
    profile = {}
    for key in ("needs", "offers", "topics", "commitments"):
        values = parsed.get(key, [])
        if not isinstance(values, list):
            profile[key] = []
            continue
        cue = {"needs": NEED_CUE, "offers": OFFER_CUE, "commitments": COMMITMENT_CUE}.get(key)
        profile[key] = [
            str(value).strip()
            for value in values
            if str(value).strip() and grounded_memory_value(value, transcript, cue)
        ]
    local_identity = explicit_identity_details(transcript)
    for key, cue in (
        ("role", r"\b(?:i am|i['’]m|my role is|work as|head of|founder|co-founder)\b"),
        ("company", r"\b(?:work at|work for|work with|company called|at|for)\b"),
    ):
        candidate = str(parsed.get(key, "")).strip()
        profile[key] = candidate if candidate and grounded_memory_value(candidate, transcript, cue) else local_identity[key]
    profile["evidence"] = [{"id": str(uuid4()), "quote": line, "conversationID": conversation_id, "capturedAt": utc_now()} for line in sentences(transcript)]
    return profile, PROVIDER.name


def identity_resolution(person: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    """Decide whether public search has enough distinct, transcript-grounded clues."""
    name = " ".join(str(person.get("name", "")).split())
    company = str(profile.get("company", "")).strip()
    role = str(profile.get("role", "")).strip()
    professional_context = next((
        str(value).strip()
        for key in ("topics", "needs", "offers")
        for value in profile.get(key, [])
        if str(value).strip()
    ), "")
    clues = []
    if len(name.split()) > 1:
        clues.append({"kind": "full_name", "label": "Full name", "value": name})
    else:
        clues.append({"kind": "first_name", "label": "First name", "value": name})
    if company:
        clues.append({"kind": "company", "label": "Company", "value": company})
    if role:
        clues.append({"kind": "role", "label": "Role", "value": role})
    if professional_context:
        clues.append({"kind": "context", "label": "Conversation context", "value": professional_context})
    enough = (len(name.split()) > 1 and bool(company or role or professional_context)) or bool(company and role)
    return {
        "status": "ready" if enough else "collecting",
        "verified": False,
        "clues": clues,
        "queryTerms": [value for value in (name, company, role, professional_context) if value],
        "message": "Enough identifying context to search safely" if enough else "Contact saved; listening for a company, role, or full name before searching",
    }


def concise_discussion(transcript: str, person_name: str, limit: int = 220) -> str:
    """Create a transcript-only summary without asking a model to invent context."""
    values = []
    introduction = re.compile(
        rf"^(?:hey[,.]?\s*)?(?:this is|my name is|i am|i['’]m)\s+{re.escape(person_name)}\b[,.!?\s-]*",
        flags=re.I,
    )
    for line in sentences(transcript):
        cleaned = introduction.sub("", " ".join(line.split())).strip(" .,-")
        if cleaned and len(tokens(cleaned)) >= 2:
            values.append(cleaned)
        if len(" ".join(values)) >= limit:
            break
    summary = " ".join(values) or " ".join(transcript.split())
    if len(summary) <= limit:
        return summary
    shortened = summary[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,.;:")
    return f"{shortened}…"


def linkedin_search_url(person: dict[str, Any], profile: dict[str, Any]) -> str:
    public_sources = [
        item.get("sourceURL") for item in profile.get("researchEvidence", [])
        if isinstance(item, dict) and "linkedin.com/" in str(item.get("sourceURL", "")).lower()
    ]
    if public_sources:
        return str(public_sources[0])
    context = " ".join(str(value) for value in profile.get("publicRoles", [])[:2])
    keywords = " ".join(part for part in (str(person.get("name", "")), context) if part).strip()
    return "https://www.linkedin.com/search/results/people/?" + urllib.parse.urlencode({"keywords": keywords})


def build_follow_up(person: dict[str, Any], conversation: dict[str, Any]) -> dict[str, Any]:
    """Build one grounded follow-up for every consented person, independent of matching."""
    profile = conversation.get("profile", {}) if isinstance(conversation.get("profile"), dict) else {}
    summary = concise_discussion(str(conversation.get("transcript", "")), str(person.get("name", "the person")))
    discussed = list(dict.fromkeys(
        str(value).strip()
        for key in ("topics", "needs", "offers", "commitments")
        for value in profile.get(key, [])
        if str(value).strip()
    ))[:6]
    reference = (discussed[0] if discussed else summary).strip(" .")
    if len(reference) > 120:
        reference = reference[:119].rsplit(" ", 1)[0].rstrip(" ,.;:") + "…"
    name = str(person.get("name", "there"))
    note = f"Hi {name}, great meeting you. I enjoyed our conversation about {reference}. I'd like to stay connected and follow up."
    if len(note) > 300:
        note = f"Hi {name}, great meeting you. I enjoyed our conversation and would like to stay connected and follow up."
    commitments = [str(value).strip() for value in profile.get("commitments", []) if str(value).strip()]
    next_step = commitments[0] if commitments else f"Review and send the prepared connection note to {name}"
    evidence = [
        item for item in profile.get("evidence", [])
        if isinstance(item, dict) and str(item.get("quote", "")).strip()
    ][:3]
    resolution = person.get("identityResolution") or identity_resolution(person, {**profile, **explicit_identity_details(str(conversation.get("transcript", "")))})
    return {
        "id": str(uuid5(NAMESPACE_URL, str(conversation.get("id", "")) + "::follow-up")),
        "personID": person.get("id"),
        "personName": name,
        "email": person.get("email") or "",
        "conversationID": conversation.get("id"),
        "summary": summary,
        "discussed": discussed,
        "role": (profile.get("publicRoles") or [""])[0],
        "identityResolution": resolution,
        "nextStep": next_step,
        "connectionNote": note,
        "profileURL": linkedin_search_url(person, profile),
        "status": "new",
        "evidence": evidence,
        "createdAt": conversation.get("timestamp") or utc_now(),
        "updatedAt": utc_now(),
        "workflowCompletedAt": utc_now(),
    }


def derive_missing_follow_ups(memory: dict[str, Any]) -> dict[str, Any]:
    """Make pre-v3 conversations visible as follow-ups without mutating storage on read."""
    people = {person.get("id"): person for person in memory.get("people", [])}
    follow_ups = {item.get("conversationID"): item for item in memory.get("followUps", [])}
    for conversation in memory.get("conversations", []):
        person = people.get(conversation.get("personID"))
        if person and conversation.get("consented") is True and conversation.get("id") not in follow_ups:
            follow_ups[conversation.get("id")] = build_follow_up(person, conversation)
    return {**memory, "followUps": sorted(follow_ups.values(), key=lambda item: str(item.get("createdAt", "")))}


class MemoryBackend:
    """Atlas-backed memory and action audit with atomic local JSON fallback."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.path = Path(env("SECONDHELLO_MEMORY_FILE") or (Path.home() / ".secondhello" / "memory.json"))
        self.mode = "Local JSON"
        self.startup_warning = ""
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
        return {"schemaVersion": 3, "people": [], "conversations": [], "followUps": [], "actions": []}

    def load(self) -> dict[str, Any]:
        if self.database is not None:
            return derive_missing_follow_ups(sanitize_memory({
                "schemaVersion": 3,
                "people": list(self.database.people.find({}, {"_id": False})),
                "conversations": list(self.database.conversations.find({}, {"_id": False, "embedding": False})),
                "followUps": list(self.database.followups.find({}, {"_id": False})),
                "actions": list(self.database.actions.find({}, {"_id": False})),
            }))
        with self.lock:
            if not self.path.exists():
                return self._empty()
            data = json.loads(self.path.read_text())
            data.setdefault("actions", [])
            return derive_missing_follow_ups(sanitize_memory(data))

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
            research_evidence = conversation.get("profile", {}).get("researchEvidence", []) or []
            for text in conversation.get("profile", {}).get("publicOffers", []) or []:
                vector, embedding_mode = PROVIDER.embedding(text, "document")
                source = next((item for item in research_evidence if item.get("sourceURL")), research_evidence[0] if research_evidence else {})
                items.append({"personID": person["id"], "personName": person["name"], "conversationID": conversation["id"], "kind": "public_offer", "text": text, "quote": source.get("quote", text), "sourceURL": source.get("sourceURL"), "sourceTitle": source.get("sourceTitle"), "embedding": vector, "embeddingMode": embedding_mode, "capturedAt": conversation["timestamp"]})
            if items:
                self.database.memory_items.insert_many(items)
            return
        with self.lock:
            memory = self.load_unlocked()
            memory["people"] = [value for value in memory["people"] if value.get("id") != person["id"]] + [person]
            memory["conversations"] = [value for value in memory["conversations"] if value.get("id") != conversation["id"]] + [conversation]
            self._write_local(memory)

    def persist_follow_up(self, follow_up: dict[str, Any]) -> None:
        if self.database is not None:
            self.database.followups.replace_one({"id": follow_up["id"]}, follow_up, upsert=True)
            return
        with self.lock:
            memory = self.load_unlocked()
            memory["followUps"] = [item for item in memory.get("followUps", []) if item.get("id") != follow_up["id"]] + [follow_up]
            self._write_local(memory)

    def load_unlocked(self) -> dict[str, Any]:
        if not self.path.exists(): return self._empty()
        data = json.loads(self.path.read_text()); data.setdefault("actions", []); data.setdefault("followUps", []); return sanitize_memory(data)

    def record_action(self, receipt: dict[str, Any]) -> None:
        if self.database is not None:
            self.database.actions.replace_one({"id": receipt["id"]}, receipt, upsert=True); return
        with self.lock:
            memory = self.load_unlocked(); memory["actions"].append(receipt); self._write_local(memory)

    def erase_all(self) -> None:
        """Delete all stored relationship data for this self-hosted instance."""
        if self.database is not None:
            for collection in (self.database.people, self.database.conversations, self.database.followups, self.database.memory_items, self.database.actions):
                collection.delete_many({})
            return
        with self.lock:
            self._write_local(self._empty())

    def vector_offers(self, query: list[float], exclude_person: str) -> list[dict[str, Any]]:
        if self.database is None or not env("MONGODB_VECTOR_INDEX"):
            return []
        try:
            pipeline = [{"$vectorSearch": {"index": env("MONGODB_VECTOR_INDEX"), "path": "embedding", "queryVector": query, "numCandidates": 100, "limit": 10}}, {"$match": {"kind": {"$in": ["offer", "public_offer"]}, "personID": {"$ne": exclude_person}}}, {"$set": {"score": {"$meta": "vectorSearchScore"}}}, {"$project": {"_id": 0, "embedding": 0}}]
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
        if not valid_person_name(state["person"]["name"]):
            return {"route": "end", "ok": False, "reason": "valid_person_name_required", "trace": trace(state, "consent_gate", "Rejected a sentence fragment as a person name before extraction or storage", "policy")}
    return {"route": routes[action], "ok": True, "trace": trace(state, "consent_gate", "Permission and input checks passed", "policy")}


def extract_tool(state: WorkflowState) -> WorkflowState:
    conversation = state["conversation"]
    profile, mode = extract_profile(conversation["transcript"], conversation["id"])
    memory_count = sum(len(profile[key]) for key in ("needs", "offers", "topics", "commitments"))
    return {"profile": profile, "route": "resolve_identity", "trace": trace(state, "extract_memory", f"Extracted {memory_count} explicit memories", mode, subject=state["person"]["name"], memoryCount=memory_count)}


def resolve_identity_tool(state: WorkflowState) -> WorkflowState:
    resolution = identity_resolution(state["person"], state["profile"])
    clue_labels = [item["label"] for item in resolution["clues"]]
    return {
        "identityResolution": resolution,
        "route": "checkpoint_contact",
        "trace": trace(
            state,
            "resolve_identity",
            resolution["message"],
            "identity safety policy",
            subject=state["person"]["name"],
            identityStatus=resolution["status"],
            clues=clue_labels,
        ),
    }


def checkpoint_contact_tool(state: WorkflowState) -> WorkflowState:
    conversation = dict(state["conversation"])
    profile = dict(state["profile"])
    previous_memory = BACKEND.load()
    previous_conversation = next((item for item in previous_memory.get("conversations", []) if item.get("id") == conversation.get("id")), {})
    previous_profile = previous_conversation.get("profile", {}) if isinstance(previous_conversation.get("profile"), dict) else {}
    for key in ("publicSummary", "publicRoles", "publicOffers", "researchEvidence", "publicCandidates", "researchAttemptedAt"):
        if key in previous_profile:
            profile[key] = previous_profile[key]
    previous_person = next((item for item in previous_memory.get("people", []) if item.get("id") == state["person"].get("id")), {})
    previous_resolution = previous_person.get("identityResolution", {}) if isinstance(previous_person.get("identityResolution"), dict) else {}
    resolution = state.get("identityResolution", {})
    if previous_profile.get("researchAttemptedAt") and previous_resolution.get("status") in {"verified", "ambiguous"}:
        resolution = previous_resolution
    conversation["profile"] = profile
    person = {**state["person"], "identityResolution": resolution}
    BACKEND.persist_capture(person, conversation)
    return {
        "person": person,
        "conversation": conversation,
        "profile": profile,
        "identityResolution": resolution,
        "route": "plan_research",
        "trace": trace(
            state,
            "save_contact",
            "Saved the person and conversation before optional public research",
            BACKEND.mode,
            subject=person["name"],
            identityStatus=state.get("identityResolution", {}).get("status"),
        ),
    }


def plan_research_tool(state: WorkflowState) -> WorkflowState:
    person = state["person"]
    profile = state["profile"]
    resolution = state.get("identityResolution", identity_resolution(person, profile))
    if profile.get("researchAttemptedAt"):
        return {
            "research_query": "",
            "route": "research",
            "trace": trace(
                state,
                "plan_public_research",
                "Reused the completed identity decision instead of repeating public research",
                "cached identity decision",
                subject=person["name"],
                identityStatus=resolution.get("status", "ambiguous"),
                cached=True,
            ),
        }
    if resolution.get("status") != "ready":
        return {
            "research_query": "",
            "route": "research",
            "trace": trace(
                state,
                "plan_public_research",
                "Deferred public search until more identifying context is spoken",
                "identity safety policy",
                subject=person["name"],
                identityStatus="collecting",
                clues=[item.get("label") for item in resolution.get("clues", [])],
            ),
        }
    context = "; ".join((profile.get("needs", []) + profile.get("offers", []) + profile.get("topics", []))[:6])
    identity_terms = "; ".join(str(value) for value in resolution.get("queryTerms", []) if str(value).strip())
    query = f'Research the public professional identity of "{person["name"]}". Identity clues: {identity_terms}. Professional context from a consented conversation: {context or state["conversation"]["transcript"][:500]}'
    enabled = PROVIDER.name == "OpenRouter" and env("SECONDHELLO_PUBLIC_RESEARCH", "1") != "0"
    detail = "Prepared a bounded identity-resolution query from name and professional context" if enabled else "Public research provider is unavailable; preserved the offline path"
    searching = {**resolution, "status": "searching", "message": f"Searching with {', '.join(resolution.get('queryTerms', [])[:3])}"}
    return {"identityResolution": searching, "research_query": query, "route": "research", "trace": trace(state, "plan_public_research", detail, PROVIDER.name, subject=person["name"], identityStatus="searching", queryTerms=resolution.get("queryTerms", [])[:4])}


def research_tool(state: WorkflowState) -> WorkflowState:
    if not state.get("research_query"):
        cached = bool(state.get("profile", {}).get("researchAttemptedAt"))
        return {
            "research": {"matched": False, "deferred": not cached, "cached": cached},
            "route": "verify_research",
            "trace": trace(
                state,
                "web_research",
                "Reused the previous identity decision" if cached else "Skipped public search because the identity is not specific enough yet",
                "cached identity decision" if cached else "identity safety policy",
                subject=state["person"]["name"],
                skipped=not cached,
                cached=cached,
                identityStatus=state.get("identityResolution", {}).get("status", "collecting"),
            ),
        }
    research, mode = PROVIDER.public_research(state.get("research_query", ""))
    source_count = len(research.get("sources", [])) if isinstance(research.get("sources"), list) else 0
    subject = state["person"]["name"]
    detail = f"Resolved public professional context with {source_count} cited source(s)" if research.get("matched") else "No unambiguous cited public identity was found"
    return {"research": research, "route": "verify_research", "trace": trace(state, "web_research", detail, mode, subject=subject, found=bool(research.get("matched")), sourceCount=source_count)}


def verify_research_tool(state: WorkflowState) -> WorkflowState:
    research = state.get("research", {})
    profile = dict(state["profile"])
    sources = research.get("sources", []) if isinstance(research.get("sources"), list) else []
    confidence = float(research.get("confidence", 0) or 0)
    accepted_identity = bool(research.get("matched")) and confidence >= float(env("PUBLIC_RESEARCH_MIN_CONFIDENCE", "0.72")) and bool(sources)
    cited_candidates = [candidate for candidate in research.get("candidateConnections", []) if isinstance(candidate, dict) and isinstance(candidate.get("source"), dict) and candidate["source"].get("url")]
    resolution = dict(state.get("identityResolution", {}))
    if research.get("cached"):
        detail = "Reused the prior cited or safely rejected identity decision"
        return {"profile": profile, "identityResolution": resolution, "route": "persist", "trace": trace(state, "verify_sources", detail, "cached identity decision", subject=state["person"]["name"], found=bool(resolution.get("verified")), candidateCount=len(profile.get("publicCandidates", [])), identityStatus=resolution.get("status"), cached=True)}
    if accepted_identity:
        evidence = [{"id": str(uuid4()), "quote": str(item.get("quote") or item.get("title") or "Public professional source"), "conversationID": state["conversation"]["id"], "capturedAt": utc_now(), "sourceURL": item.get("url"), "sourceTitle": item.get("title")} for item in sources]
        profile["publicSummary"] = str(research.get("summary", "")).strip() or None
        profile["publicRoles"] = [str(value).strip() for value in research.get("roles", []) if str(value).strip()][:6]
        profile["publicOffers"] = [str(value).strip() for value in research.get("offers", []) if str(value).strip()][:8]
        profile["researchEvidence"] = evidence
        identity_detail = f"accepted identity at {confidence:.0%} confidence"
        resolution.update({"status": "verified", "verified": True, "confidence": confidence, "message": f"Public identity verified from {len(sources)} cited source(s)"})
    else:
        profile["publicSummary"] = None; profile["publicRoles"] = []; profile["publicOffers"] = []; profile["researchEvidence"] = []
        if research.get("deferred"):
            resolution.update({"status": "collecting", "verified": False, "message": "Contact saved; public identity remains unverified until more context is available"})
            identity_detail = "kept identity unverified while collecting more context"
        else:
            resolution.update({"status": "ambiguous", "verified": False, "confidence": confidence, "message": "Search results were not specific enough to attach safely"})
            profile["researchAttemptedAt"] = utc_now()
            identity_detail = "rejected ambiguous identity enrichment"
    if accepted_identity:
        profile["researchAttemptedAt"] = utc_now()
    profile["publicCandidates"] = cited_candidates[:3]
    detail = f"{identity_detail}; accepted {len(profile['publicCandidates'])} independently cited opportunity candidate(s)"
    return {"profile": profile, "identityResolution": resolution, "route": "persist", "trace": trace(state, "verify_sources", detail, "evidence policy", subject=state["person"]["name"], found=accepted_identity, candidateCount=len(profile["publicCandidates"]), identityStatus=resolution.get("status"))}


def persist_tool(state: WorkflowState) -> WorkflowState:
    conversation = dict(state["conversation"]); conversation["profile"] = state["profile"]
    person = dict(state["person"])
    person["identityResolution"] = state.get("identityResolution", {})
    if state["profile"].get("publicSummary"):
        person["publicSummary"] = state["profile"]["publicSummary"]
        person["publicRoles"] = state["profile"].get("publicRoles", [])
        person["researchSources"] = [{"url": item.get("sourceURL"), "title": item.get("sourceTitle")} for item in state["profile"].get("researchEvidence", [])]
    BACKEND.persist_capture(person, conversation)
    discussed = (state["profile"].get("topics", []) + state["profile"].get("needs", []) + state["profile"].get("offers", []))[:4]
    return {"person": person, "conversation": conversation, "route": "follow_up", "trace": trace(state, "persist_memory", "Stored consent receipt, evidence, and structured memory", BACKEND.mode, subject=person["name"], topics=discussed)}


def prepare_follow_up_tool(state: WorkflowState) -> WorkflowState:
    follow_up = build_follow_up(state["person"], state["conversation"])
    BACKEND.persist_follow_up(follow_up)
    return {
        "followUp": follow_up,
        "route": "end",
        "trace": trace(
            state,
            "prepare_follow_up",
            "Prepared a grounded follow-up tracker and editable connection note",
            BACKEND.mode,
            subject=follow_up["personName"],
            discussed=follow_up["discussed"],
        ),
    }


def profile_map(memory: dict[str, Any]) -> dict[str, dict[str, Any]]:
    profiles: dict[str, dict[str, Any]] = {}
    for conversation in memory.get("conversations", []):
        current = profiles.setdefault(conversation.get("personID", ""), {"needs": [], "offers": [], "evidence": [], "publicCandidates": []})
        profile = conversation.get("profile", {})
        for key in ("needs", "offers"):
            current[key].extend(value for value in profile.get(key, []) if value not in current[key])
        current["evidence"].extend(profile.get("evidence", []))
        for offer in profile.get("publicOffers", []) or []:
            if offer not in current["offers"]: current["offers"].append(offer)
        current["evidence"].extend(profile.get("researchEvidence", []) or [])
        for candidate in profile.get("publicCandidates", []) or []:
            if candidate not in current["publicCandidates"]: current["publicCandidates"].append(candidate)
    return profiles


def evidence_for(profile: dict[str, Any], text: str) -> dict[str, Any] | None:
    return next((item for item in profile.get("evidence", []) if text.lower() in item.get("quote", "").lower()), None)


def match_tool(state: WorkflowState) -> WorkflowState:
    memory = BACKEND.load()
    people = {person["id"]: person for person in memory.get("people", [])}
    profiles = profile_map(memory)
    opportunities: list[dict[str, Any]] = []
    search_mode = "Local semantic"
    minimum = float(env("SECONDHELLO_MATCH_THRESHOLD", "0.18"))
    need_texts = list(dict.fromkeys(
        need for profile in profiles.values() for need in profile.get("needs", [])
    ))
    document_texts = list(dict.fromkeys(
        [offer for profile in profiles.values() for offer in profile.get("offers", [])]
        + [
            str(candidate.get("supportedCapability", "")).strip() + " " + str(candidate.get("rationale", "")).strip()
            for profile in profiles.values()
            for candidate in profile.get("publicCandidates", [])
            if str(candidate.get("supportedCapability", "")).strip()
        ]
    ))
    need_vectors, embedding_mode = PROVIDER.embeddings(need_texts, "query")
    document_vectors, _ = PROVIDER.embeddings(document_texts, "document")
    query_embeddings = dict(zip(need_texts, need_vectors))
    document_embeddings = dict(zip(document_texts, document_vectors))
    for recipient_id, recipient_profile in profiles.items():
        for need in recipient_profile.get("needs", []):
            query = query_embeddings[need]
            need_evidence = evidence_for(recipient_profile, need)
            if not need_evidence:
                continue
            atlas = BACKEND.vector_offers(query, recipient_id)
            if atlas:
                candidates = [(float(item.get("score", 0)), item["personID"], item["text"], {"id": str(uuid4()), "quote": item.get("quote", item["text"]), "conversationID": item.get("conversationID", ""), "capturedAt": item.get("capturedAt", utc_now()), "sourceURL": item.get("sourceURL"), "sourceTitle": item.get("sourceTitle")}) for item in atlas]
                search_mode = "Atlas Vector Search"
            else:
                candidates = []
                for connector_id, connector_profile in profiles.items():
                    if connector_id == recipient_id: continue
                    for offer in connector_profile.get("offers", []):
                        offer_vector = document_embeddings[offer]
                        candidates.append((cosine(query, offer_vector), connector_id, offer, evidence_for(connector_profile, offer)))
            for score, connector_id, offer, offer_evidence in sorted(candidates, key=lambda item: item[0], reverse=True)[:1]:
                if score < minimum or not offer_evidence or connector_id not in people or recipient_id not in people: continue
                opportunities.append({
                    "id": str(uuid4()), "recipientID": recipient_id, "recipientName": people[recipient_id]["name"], "recipientEmail": people[recipient_id].get("email"),
                    "connectorID": connector_id, "connectorName": people[connector_id]["name"], "connectorEmail": people[connector_id].get("email"),
                    "need": need, "offer": offer, "score": round(score, 3), "needEvidence": need_evidence,
                    "offerEvidence": offer_evidence, "searchMode": search_mode + (" + cited public research" if offer_evidence.get("sourceURL") else ""),
                })
            for candidate in recipient_profile.get("publicCandidates", []):
                source = candidate.get("source", {}) if isinstance(candidate.get("source"), dict) else {}
                capability = str(candidate.get("supportedCapability", "")).strip()
                if not capability or not source.get("url"): continue
                candidate_text = capability + " " + str(candidate.get("rationale", "")).strip()
                candidate_vector = document_embeddings[candidate_text]
                score = cosine(query, candidate_vector)
                if score < minimum: continue
                candidate_id = str(uuid5(NAMESPACE_URL, str(source["url"]) + str(candidate.get("name", ""))))
                opportunities.append({
                    "id": str(uuid5(NAMESPACE_URL, recipient_id + candidate_id + need)),
                    "recipientID": recipient_id, "recipientName": people[recipient_id]["name"], "recipientEmail": people[recipient_id].get("email"),
                    "connectorID": candidate_id, "connectorName": str(candidate.get("name", "Public candidate")), "connectorEmail": None,
                    "need": need, "offer": capability, "score": round(score, 3), "needEvidence": need_evidence,
                    "offerEvidence": {"id": str(uuid4()), "quote": str(source.get("quote") or candidate.get("rationale") or capability), "conversationID": "", "capturedAt": utc_now(), "sourceURL": source.get("url"), "sourceTitle": source.get("title")},
                    "searchMode": "OpenRouter Web Search · public candidate",
                })
    subject = state.get("person", {}).get("name") or "your network"
    return {"memory": memory, "opportunities": opportunities, "route": "rank", "trace": trace(state, "find_introductions", f"Compared explicit needs against transcript and cited public capability signals; found {len(opportunities)} candidate(s)", search_mode, subject=subject, opportunityCount=len(opportunities))}


def rank_tool(state: WorkflowState) -> WorkflowState:
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for opportunity in state.get("opportunities", []):
        key = (opportunity["recipientID"], opportunity["connectorID"])
        if key not in unique or opportunity["score"] > unique[key]["score"]: unique[key] = opportunity
    values = sorted(unique.values(), key=lambda item: item["score"], reverse=True)
    subject = state.get("person", {}).get("name") or "your network"
    return {"opportunities": values, "route": "end", "trace": trace(state, "rank_opportunities", f"Deduplicated and ranked {len(values)} evidence-backed opportunity(s)", "deterministic policy", subject=subject, opportunityCount=len(values))}


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
    return {key: state[key] for key in ("ok", "reason", "person", "profile", "identityResolution", "conversation", "followUp", "opportunities", "draft", "action_receipt", "trace") if key in state}


def run_local(initial: WorkflowState) -> WorkflowState:
    state = dict(initial); state.update(validate_and_route(state))
    while state.get("route") != "end":
        node = {"extract": extract_tool, "resolve_identity": resolve_identity_tool, "checkpoint_contact": checkpoint_contact_tool, "plan_research": plan_research_tool, "research": research_tool, "verify_research": verify_research_tool, "persist": persist_tool, "follow_up": prepare_follow_up_tool, "match": match_tool, "rank": rank_tool, "draft": draft_tool, "record_action": record_action_tool}[state["route"]]
        state.update(node(state))
    return state


def build_graph():
    if StateGraph is None: return None
    graph = StateGraph(WorkflowState)
    graph.add_node("guard", validate_and_route); graph.add_node("extract", extract_tool); graph.add_node("resolve_identity", resolve_identity_tool); graph.add_node("checkpoint_contact", checkpoint_contact_tool); graph.add_node("plan_research", plan_research_tool); graph.add_node("research", research_tool); graph.add_node("verify_research", verify_research_tool); graph.add_node("persist", persist_tool); graph.add_node("follow_up", prepare_follow_up_tool); graph.add_node("match", match_tool); graph.add_node("rank", rank_tool); graph.add_node("draft", draft_tool); graph.add_node("record_action", record_action_tool)
    graph.add_edge(START, "guard")
    graph.add_conditional_edges("guard", lambda state: state.get("route", "end"), {"extract": "extract", "match": "match", "draft": "draft", "record_action": "record_action", "end": END})
    graph.add_edge("extract", "resolve_identity"); graph.add_edge("resolve_identity", "checkpoint_contact"); graph.add_edge("checkpoint_contact", "plan_research"); graph.add_edge("plan_research", "research"); graph.add_edge("research", "verify_research"); graph.add_edge("verify_research", "persist"); graph.add_edge("persist", "follow_up"); graph.add_edge("follow_up", END); graph.add_edge("match", "rank"); graph.add_edge("rank", END); graph.add_edge("draft", END); graph.add_edge("record_action", END)
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
        "storageFallback": BACKEND.mode != "MongoDB Atlas",
        "storageNotice": getattr(BACKEND, "startup_warning", "") if BACKEND.mode != "MongoDB Atlas" else "",
        "provider": PROVIDER.name,
        "vectorSearch": bool(BACKEND.database is not None and env("MONGODB_VECTOR_INDEX")),
        "publicResearch": PROVIDER.name == "OpenRouter" and env("SECONDHELLO_PUBLIC_RESEARCH", "1") != "0",
        "voiceAgentConfigured": bool(env("ELEVENLABS_API_KEY") and env("ELEVENLABS_AGENT_ID")),
        "realtimeTranscriptionConfigured": bool(env("ELEVENLABS_API_KEY")),
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


def elevenlabs_scribe_token() -> tuple[int, dict[str, Any]]:
    """Mint a short-lived browser token for ElevenLabs Scribe realtime STT."""
    api_key = env("ELEVENLABS_API_KEY")
    if not api_key:
        return 503, {"ok": False, "reason": "elevenlabs_api_key_required"}
    request = urllib.request.Request(
        "https://api.elevenlabs.io/v1/single-use-token/realtime_scribe",
        headers={"xi-api-key": api_key, "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=float(env("PROVIDER_TIMEOUT_SECONDS", "12"))) as response:
            payload = json.loads(response.read())
        token = str(payload.get("token", "")).strip()
        if not token:
            return 502, {"ok": False, "reason": "elevenlabs_scribe_token_unavailable"}
        return 200, {"ok": True, "token": token, "modelId": "scribe_v2_realtime"}
    except (urllib.error.URLError, TimeoutError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return 502, {"ok": False, "reason": "elevenlabs_scribe_token_unavailable"}


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
    # Keep direct invocation safe and compatible while routing through the
    # production HTTP boundary (auth, bounded requests, SSE, and static web).
    from production_server import serve as production_serve

    production_serve(env("SECONDHELLO_HOST", "127.0.0.1"), int(env("SECONDHELLO_PORT", "8765")))
