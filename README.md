# Second Hello

Second Hello is a self-hostable, consent-first networking memory agent. It turns explicitly permitted conversations into structured memory, researches public professional context, finds evidence-backed introductions, and prepares a human-reviewed handoff. It never sends a message autonomously.

The production surface is a browser/PWA client served by the local agent service. The macOS client remains available as an optional native client. Each deployment owns its data and provider credentials; no Second Hello-hosted account is required.

## Self-hosted production deployment

The supported deployment is one container with a durable volume. Copy the sanitized configuration, create a strong instance token, then build and run:

```zsh
cd /Users/eswaravegi/projects/resume/SecondHello
cp .env.example .env
openssl rand -hex 32
# Put the generated value in .env as SECONDHELLO_AUTH_TOKEN=...
docker compose up --build
```

Open `http://127.0.0.1:8765`. Paste the same bearer token into **Settings & deployment**. Configure Atlas, ElevenLabs, OpenRouter, or Fireworks in `.env`; the app stays usable with local JSON and deterministic local logic when those values are absent. For an internet-facing deployment, terminate TLS at a reverse proxy and set `SECONDHELLO_CORS_ORIGINS` to the exact browser origins. Do not expose the plain HTTP port directly to the public internet.

For a local developer run without Docker:

```zsh
cd /Users/eswaravegi/projects/resume/SecondHello/web
pnpm install --frozen-lockfile
pnpm exec vite build
cd ..
scripts/run-server.sh
```

`scripts/run-server.sh` uses the FastAPI/Uvicorn ASGI entrypoint when the production dependencies are installed and falls back to the standard-library boundary for zero-dependency local runs. `SECONDHELLO_ENV=production` requires `SECONDHELLO_AUTH_TOKEN`; development mode is intentionally unauthenticated only on loopback.

The optional local MongoDB profile is:

```zsh
docker compose --profile mongo up --build
```

Set `MONGODB_URI=mongodb://mongo:27017/secondhello` when using it. Atlas remains the recommended durable store for multi-process deployments; local JSON is designed for one self-hosted instance and is protected by atomic writes.

The production API provides:

- `GET /api/health` and `GET /api/readyz` for liveness/readiness.
- `POST /api/workflow` for a durable workflow result.
- `POST /api/workflow/events` for streamed LangGraph node events over SSE.
- `GET /api/memory/export` for a user-authorized export.
- `DELETE /api/memory` only with `X-SecondHello-Confirm: DELETE_ALL`.
- `GET /api/voice/signed-url` for a short-lived ElevenLabs session URL; the provider key never reaches the browser.
- `POST /api/voice/scribe-token` for a single-use ElevenLabs Scribe realtime transcription token; the provider key never reaches the browser.

Run the complete release check before distributing a build:

```zsh
./scripts/check-release.sh
```

## The 90-second demo

The demo tells one complete story rather than touring settings:

1. Open the local live room, arm consent, and choose **Start live room**. The private ElevenLabs voice agent and Scribe realtime speech-to-text connect together; partial speech appears immediately while committed turns enter the transcript.
2. Keep talking. Once a clear name and meaningful context are present, the LangGraph workflow starts automatically in the background. Its guard, extraction, public research, evidence, persistence, and matching events appear in the **Live agent activity** sidecar while the room remains open.
3. Stop the room only when you want the final sync. There is no “Remember person” submit step; consented turns are saved continuously and the final state updates People, evidence, and Opportunities.
4. Choose an opportunity to review the editable introduction draft. The mail handoff remains human-approved and nothing is sent automatically.
4. Choose **Show evidence** to reveal the exact source excerpts behind both sides of the match.
5. Choose **Prepare introduction**, review the editable draft, explicitly approve the handoff, and open a real draft in the default mail app. The mail app remains responsible for sending.

The fixture uses reserved `example.com` addresses so an accidental send cannot contact a real person. Demo content lives in `Sources/SecondHello/Resources/demo_scenario.json`; matching and workflow code contain no named-person branches.

## Run offline

No Python packages, API keys, database, or network are required:

```zsh
cd /Users/eswaravegi/projects/resume/SecondHello
swift run
```

For live microphone transcription, build the application bundle so macOS receives the required privacy descriptions:

```zsh
./scripts/package-app.sh
open .build/SecondHello.app
```

The first live session asks for microphone access. Capture starts only after both the in-app consent gate and macOS permission succeed. An authenticated realtime agent is used when the local server can issue a signed session URL. If that path is unavailable, Apple Speech provides the editable transcript fallback and may also request speech-recognition access. Withdrawing consent stops the active audio engine and queued agent audio immediately. Apple Speech runs on-device when the current locale supports it and uses the system speech service otherwise.

Offline Demo Mode uses deterministic extraction and semantic vectors plus atomic JSON persistence at:

```text
~/Library/Application Support/SecondHello/memory.json
```

## Run the live LangGraph agent

```zsh
cd /Users/eswaravegi/projects/resume/SecondHello
python3 -m venv .venv
.venv/bin/pip install -r server/requirements.txt
SECONDHELLO_MEMORY_FILE=/tmp/secondhello-demo-memory.json .venv/bin/python server/main.py
```

In a second terminal:

```zsh
cd /Users/eswaravegi/projects/resume/SecondHello
SECONDHELLO_SERVER_URL=http://127.0.0.1:8765 swift run
```

You can also save the URL in **Trust center**. Check the running service with:

```zsh
curl http://127.0.0.1:8765/health
```

## MongoDB Atlas

The server uses real Atlas collections when a connection succeeds: `people`, `conversations`, `memory_items`, and `actions`. It writes one vectorized document per need/offer/topic/commitment and uses `$vectorSearch` when an index is configured. A failed or absent Atlas connection falls back to atomic local JSON without breaking the demo.

The simplest setup is to edit the local `.env` file. It is ignored by Git. Paste only the database-user password after `MONGODB_PASSWORD=`; the server safely URL-encodes it and constructs the Atlas URI from the remaining settings.

```zsh
cd /Users/eswaravegi/projects/resume/SecondHello
.venv/bin/python server/main.py
```

You can alternatively set a complete `MONGODB_URI`; exported shell values take precedence over `.env`, and `MONGODB_URI` takes precedence over the split username/password/host settings. Use `.env.example` as the sanitized configuration reference.

For deterministic 96-dimensional embeddings, create an Atlas Vector Search index on `memory_items` with:

```json
{
  "fields": [
    {
      "type": "vector",
      "path": "embedding",
      "numDimensions": 96,
      "similarity": "cosine"
    }
  ]
}
```

The supplied Fireworks configuration uses Qwen3 Embedding 8B at 4096 dimensions. When that provider is enabled, create the same index with `numDimensions: 4096` instead. If Vector Search is unavailable or mismatched, the agent visibly reports and uses local semantic ranking.

## Fireworks or OpenRouter

Models are configuration, not application logic. A provider is enabled only when both its key and model are supplied. Chat calls perform structured extraction and introduction drafting; embedding calls perform semantic matching. Any timeout, invalid JSON, or provider failure returns to deterministic local tools.

When both providers are configured, select one explicitly with `SECONDHELLO_PROVIDER=fireworks` or `SECONDHELLO_PROVIDER=openrouter`. The local demo configuration uses the verified OpenRouter route; switching providers does not require a code change.

These values can also be entered in the ignored `.env` file.

Fireworks:

```zsh
export FIREWORKS_API_KEY='…'
export FIREWORKS_MODEL='accounts/fireworks/models/deepseek-v4-flash'
export FIREWORKS_EMBEDDING_MODEL='fireworks/qwen3-embedding-8b'
export FIREWORKS_EMBEDDING_DIMENSIONS='4096'
```

OpenRouter:

```zsh
export OPENROUTER_API_KEY='…'
export OPENROUTER_MODEL='~openai/gpt-latest'
export OPENROUTER_EMBEDDING_MODEL='qwen/qwen3-embedding-8b'
export OPENROUTER_EMBEDDING_DIMENSIONS='4096'

# Evidence-backed public professional research (enabled by default)
export SECONDHELLO_PUBLIC_RESEARCH='1'
export OPENROUTER_SEARCH_ENGINE='auto'
export OPENROUTER_SEARCH_RESULTS='6'
export OPENROUTER_SEARCH_MAX_USES='3'
export PUBLIC_RESEARCH_MIN_CONFIDENCE='0.72'
```

Optional endpoint overrides are `FIREWORKS_CHAT_URL`, `FIREWORKS_EMBEDDING_URL`, `OPENROUTER_CHAT_URL`, and `OPENROUTER_EMBEDDING_URL`. Other server settings are `SECONDHELLO_HOST`, `SECONDHELLO_PORT`, `SECONDHELLO_MEMORY_FILE`, `SECONDHELLO_MATCH_THRESHOLD`, `MONGODB_TIMEOUT_MS`, and `PROVIDER_TIMEOUT_SECONDS`.

The native voice client uses raw PCM by default because it is reliable across built-in, USB, and virtual microphones. Set `SECONDHELLO_VOICE_PROCESSING_ENABLED=1` only for a microphone route known to work with macOS voice processing.

## ElevenLabs realtime voice

The native app never receives the ElevenLabs API key. It asks the local server for `/elevenlabs/signed-url`, then opens the short-lived WebSocket directly. Microphone PCM, agent audio, transcripts, interruptions, and ping/pong events travel over that signed session. Failure to obtain or maintain the session leaves the saved-memory workflow intact and starts the Apple Speech fallback for capture.

Create a key in the ElevenLabs dashboard under **Developers → API Keys → Create API Key**. Restrict it to the API capabilities needed by the Conversational AI agent and set a small credit quota. Copy it when it is created—the complete value is not shown again—then add it only to the ignored `.env` file:

```dotenv
ELEVENLABS_API_KEY=your_new_key
ELEVENLABS_AGENT_ID=your_agent_id
```

The configured demo agent ID is already present in the local `.env`; only the key is missing. Do not paste the key into Swift source, `UserDefaults`, a shell-history command, or this README. Restart `server/main.py` after editing `.env`, then verify only the non-secret readiness flag:

```zsh
curl -s http://127.0.0.1:8765/health
# Expected when both values are loaded: "voiceAgentConfigured": true
```

The optional imported-file transcription and spoken-briefing tools are legacy direct API features. They use a separate key saved in macOS Keychain from the collapsed **Legacy file transcription and briefing** section in **Trust center**; realtime conversation does not use that client-side key.

## Architecture

The LangGraph path is:

```text
request → consent_gate ─┬─ capture → extract_memory → plan_public_research → web_research
                        │             → verify_sources → persist_memory → find_introductions → rank_opportunities
                        ├─ match ───────────────────→ find_introductions → rank_opportunities
                        ├─ draft ───────────────────→ compose_introduction
                        └─ approved Mail handoff ───→ record_action
```

- `server/main.py`: LangGraph nodes, real provider adapters, Atlas/local persistence, vector retrieval, action audit, HTTP API
- `Services.swift`: signed ElevenLabs WebSocket/audio client, Apple Speech fallback, local server client, legacy Keychain tools, and safe system mail-draft handoff
- `MemoryStore.swift`: offline-first cache, server orchestration, generic semantic fallback
- `SecondHelloApp.swift`: the demo narrative, evidence UI, tool receipts, and human approval gate
- `Resources/demo_scenario.json`: optional demo fixture, separated from product logic

## Tests and packaging

```zsh
cd /Users/eswaravegi/projects/resume/SecondHello
swift test
python3 -m unittest discover -s server -p 'test_*.py' -v
./scripts/package-app.sh
open .build/SecondHello.app
```

Tests cover the pre-storage consent boundary, private live-listener lifecycle, ElevenLabs PCM format handling and private-URL guard, local durability, configuration-driven fixture extraction, generic semantic matching, tool order, editable draft creation, and the non-sending `mailto:` handoff.

## Remaining limitations

- Atlas Vector Search index creation is an explicit deployment step because dimensions depend on the configured embedding model.
- Provider, live Atlas, and live ElevenLabs calls require user-supplied credentials and available credits; automated tests exercise their boundaries and deterministic fallback without making paid calls.
- A real microphone-to-agent session must still be acceptance-tested after the ElevenLabs key is added. Audio echo cancellation and behavior with speakers versus headphones depend on the current macOS audio route and ElevenLabs turn-taking configuration.
- The default mail app receives a draft through the system `mailto:` handler. Second Hello cannot verify whether the user later edits, discards, or sends it.
- The local cache and Atlas are additive stores; this demo does not yet implement conflict resolution between multiple Macs.
