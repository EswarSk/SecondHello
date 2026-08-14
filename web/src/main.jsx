import React, { useCallback, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  ConversationProvider,
  useScribe,
  useConversation,
  useConversationControls,
  useConversationInput,
  useConversationStatus,
} from "@elevenlabs/react";
import "./styles.css";

const API = (import.meta.env.VITE_API_BASE_URL || "").replace(/\/$/, "");
const AUTH_KEY = "secondhello.authToken";
const USER_KEY = "secondhello.userId";

const icons = {
  spark: "✦",
  mic: "◉",
  pause: "Ⅱ",
  stop: "■",
  arrow: "↗",
  check: "✓",
  lock: "⌑",
};

function apiPath(path) {
  return `${API}${path}`;
}

function authHeaders(token, json = false) {
  return {
    ...(json ? { "Content-Type": "application/json" } : {}),
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
}

function displayError(error) {
  return error instanceof Error ? error.message : String(error || "Something went wrong");
}

function speakerName(transcript) {
  const match = transcript.match(/(?:my name is|this is|i am|i'm|im)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})/i);
  return match ? match[1].trim() : "";
}

function eventLabel(event) {
  const trace = event.trace;
  if (trace?.detail) return trace.detail;
  const labels = {
    guard: "Consent and input checks passed",
    extract: "Extracting explicit needs, offers, and topics",
    plan_research: "Preparing a bounded public research query",
    research: "Researching public professional sources",
    verify_research: "Verifying identity and source evidence",
    persist: "Saving consented memory to the relationship graph",
    match: "Searching for complementary opportunities",
    rank: "Ranking evidence-backed introductions",
    draft: "Preparing a human-reviewed connection note",
    record_action: "Recording the approved handoff",
  };
  return labels[event.node] || event.node || "Agent update";
}

function Provider({ children }) {
  const [muted, setMuted] = useState(false);
  const handleError = useCallback((error) => console.error("Second Hello voice error", error), []);
  return (
    <ConversationProvider isMuted={muted} onMutedChange={setMuted} onError={handleError}>
      {children}
    </ConversationProvider>
  );
}

function App() {
  const [token, setToken] = useState(() => localStorage.getItem(AUTH_KEY) || "");
  const [userId] = useState(() => {
    const current = localStorage.getItem(USER_KEY);
    if (current) return current;
    const created = globalThis.crypto?.randomUUID?.() || `user-${Date.now()}`;
    localStorage.setItem(USER_KEY, created);
    return created;
  });
  const [health, setHealth] = useState(null);
  const [memory, setMemory] = useState({ people: [], conversations: [], actions: [] });
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [consent, setConsent] = useState(false);
  const [transcript, setTranscript] = useState("");
  const [partialTranscript, setPartialTranscript] = useState("");
  const [activity, setActivity] = useState([]);
  const [opportunities, setOpportunities] = useState([]);
  const [research, setResearch] = useState(null);
  const [phase, setPhase] = useState("idle");
  const [statusText, setStatusText] = useState("Arm consent to begin listening");
  const [error, setError] = useState("");
  const [selectedOpportunity, setSelectedOpportunity] = useState(null);
  const [draft, setDraft] = useState(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [tokenDraft, setTokenDraft] = useState(token);
  const [voiceMessages, setVoiceMessages] = useState([]);
  const [workflowBusy, setWorkflowBusy] = useState(false);

  const transcriptRef = useRef("");
  const nameRef = useRef("");
  const phaseRef = useRef("idle");
  const consentRef = useRef(false);
  const personRef = useRef(null);
  const conversationRef = useRef(null);
  const workflowTimerRef = useRef(null);
  const workflowInFlightRef = useRef(false);
  const workflowQueuedRef = useRef(false);
  const finishRequestedRef = useRef(false);
  const lastSegmentRef = useRef("");

  useEffect(() => { transcriptRef.current = transcript; }, [transcript]);
  useEffect(() => { nameRef.current = name; }, [name]);
  useEffect(() => { phaseRef.current = phase; }, [phase]);
  useEffect(() => { consentRef.current = consent; }, [consent]);

  const { status: voiceStatus } = useConversationStatus();
  const { startSession, endSession } = useConversationControls();
  const { isMuted, setMuted } = useConversationInput();
  const scribe = useScribe({
    modelId: "scribe_v2_realtime",
    onPartialTranscript: (data) => {
      setPartialTranscript(String(data?.text || ""));
      setStatusText("Live speech-to-text is updating");
    },
    onCommittedTranscript: (data) => handleTranscriptSegment(data?.text || ""),
    onError: (scribeError) => {
      appendActivity({ type: "agent.notice", trace: { detail: `Realtime transcription issue: ${displayError(scribeError)}`, mode: "ElevenLabs Scribe" } });
    },
  });
  const conversation = useConversation({
    onMessage: (message) => {
      const source = message?.source || message?.type;
      const text = message?.message || message?.text || message?.user_transcription_event?.user_transcript || "";
      if (!text || (source && source !== "user" && source !== "user_transcript" && !message?.user_transcription_event)) return;
      handleTranscriptSegment(text);
    },
    onStatusChange: (value) => {
      const status = typeof value === "string" ? value : value?.status;
      if (status) setStatusText(status === "connected" ? "Live voice session is active" : `Voice session ${status}`);
    },
    onModeChange: (value) => {
      const mode = typeof value === "string" ? value : value?.mode;
      if (mode) setStatusText(mode === "speaking" ? "Second Hello is responding" : "Listening quietly in the background");
    },
    onVadScore: (value) => {
      const score = typeof value === "number" ? value : value?.score;
      if (typeof score === "number" && score > 0.1) setStatusText("Microphone signal detected · listening");
    },
  });

  const loadState = useCallback(async ({ includeMatches = true } = {}) => {
    setError("");
    try {
      const healthResponse = await fetch(apiPath("/api/health"), { headers: authHeaders(token) });
      const healthBody = await healthResponse.json();
      setHealth(healthBody);
      const memoryResponse = await fetch(apiPath("/api/memory"), { headers: authHeaders(token) });
      if (memoryResponse.ok) {
        setMemory(await memoryResponse.json());
        // Rehydrate the tracker on reload. Persisted people are not useful if
        // their current evidence-backed matches disappear until the next capture.
        if (includeMatches) {
          const matchResponse = await fetch(apiPath("/api/workflow"), {
            method: "POST",
            headers: authHeaders(token, true),
            body: JSON.stringify({ action: "match" }),
          });
          if (matchResponse.ok) {
            const matchBody = await matchResponse.json();
            setOpportunities(matchBody.opportunities || []);
          }
        }
      }
    } catch (caught) {
      setError(`Agent server unavailable: ${displayError(caught)}`);
    }
  }, [token]);

  useEffect(() => { loadState(); }, [loadState]);

  const people = memory.people || [];
  const latestPerson = people[people.length - 1];
  const currentConversation = memory.conversations?.find((item) => item.personID === latestPerson?.id) || null;
  const profile = currentConversation?.profile || null;
  const modeLabel = health?.provider || "Local deterministic";
  const storageLabel = health?.storage || "Local JSON";

  const appendActivity = useCallback((event) => {
    setActivity((items) => {
      const next = [...items, { ...event, id: `${event.type}-${event.node || "workflow"}-${Date.now()}-${Math.random()}` }];
      return next.slice(-12);
    });
  }, []);

  function handleTranscriptSegment(rawText) {
    const text = String(rawText || "").trim();
    if (!text || text === lastSegmentRef.current) return;
    lastSegmentRef.current = text;
    setVoiceMessages((items) => [...items.slice(-30), { text, at: new Date().toISOString() }]);
    const nextTranscript = transcriptRef.current ? `${transcriptRef.current}\n${text}` : text;
    transcriptRef.current = nextTranscript;
    setTranscript(nextTranscript);
    const detectedName = nameRef.current || speakerName(nextTranscript);
    if (detectedName && !nameRef.current) {
      nameRef.current = detectedName;
      setName(detectedName);
    }
    if (phaseRef.current === "listening") scheduleLiveWorkflow(nextTranscript, detectedName);
  }

  function scheduleLiveWorkflow(textSnapshot, nameSnapshot) {
    if (!consentRef.current) return;
    if (!nameSnapshot) {
      setStatusText("Listening · waiting for a clear introduction");
      return;
    }
    const enoughContext = textSnapshot.trim().length >= 24 || textSnapshot.trim().split(/\s+/).length >= 6;
    if (!enoughContext) return;
    if (workflowTimerRef.current) window.clearTimeout(workflowTimerRef.current);
    workflowTimerRef.current = window.setTimeout(() => {
      runLiveWorkflow(textSnapshot, nameSnapshot);
    }, 900);
  }

  async function streamWorkflow(payload) {
    const response = await fetch(apiPath("/api/workflow/events"), { method: "POST", headers: authHeaders(token, true), body: JSON.stringify(payload) });
    if (!response.ok || !response.body) throw new Error(`Workflow request failed (${response.status})`);
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let result = null;
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const messages = buffer.split("\n\n");
      buffer = messages.pop() || "";
      for (const message of messages) {
        const dataLine = message.split("\n").find((line) => line.startsWith("data: "));
        if (!dataLine) continue;
        const event = JSON.parse(dataLine.slice(6));
        appendActivity(event);
        if (event.type === "node.completed") {
          if (event.research) setResearch(event.research);
          if (event.opportunities) setOpportunities(event.opportunities);
          setStatusText(eventLabel(event));
        }
        if (event.type === "workflow.completed") {
          result = event.result;
          if (!result?.ok) throw new Error(result?.reason || "Workflow rejected the capture");
          if (result.opportunities) setOpportunities(result.opportunities);
        }
        if (event.type === "workflow.failed") throw new Error(event.message || "Workflow failed");
      }
      if (done) break;
    }
    return result;
  }

  async function runLiveWorkflow(textSnapshot, nameSnapshot, force = false) {
    if (!consentRef.current || !nameSnapshot || !textSnapshot.trim()) return;
    if (!force && phaseRef.current !== "listening") return;
    if (workflowInFlightRef.current) {
      workflowQueuedRef.current = true;
      return;
    }
    workflowInFlightRef.current = true;
    setWorkflowBusy(true);
    setStatusText("Agent is processing this conversation in parallel…");
    if (!personRef.current) {
      const personID = globalThis.crypto?.randomUUID?.() || `person-${Date.now()}`;
      const conversationID = globalThis.crypto?.randomUUID?.() || `conversation-${Date.now()}`;
      personRef.current = { id: personID, name: nameSnapshot, email: email.trim(), createdAt: new Date().toISOString() };
      conversationRef.current = { id: conversationID, personID: personID, timestamp: new Date().toISOString(), consented: true, consentedAt: new Date().toISOString() };
    }
    const payload = {
      action: "capture",
      person: { ...personRef.current, name: nameSnapshot, email: email.trim() },
      conversation: { ...conversationRef.current, transcript: textSnapshot, profile: {} },
    };
    try {
      const result = await streamWorkflow(payload);
      await loadState({ includeMatches: false });
      if (finishRequestedRef.current && !workflowQueuedRef.current && transcriptRef.current === textSnapshot) {
        finishRequestedRef.current = false;
        setPhase("saved");
        setStatusText("Saved live · People, evidence, and opportunities are updated");
      } else if (!finishRequestedRef.current) {
        setStatusText(result?.opportunities ? "Agent synced · still listening for the next turn" : "Agent is listening for more context");
      }
    } catch (caught) {
      appendActivity({ type: "agent.notice", trace: { detail: `Live workflow paused: ${displayError(caught)}`, mode: "LangGraph" } });
      setError(displayError(caught));
    } finally {
      workflowInFlightRef.current = false;
      setWorkflowBusy(false);
      const needsFinalSync = workflowQueuedRef.current || transcriptRef.current !== textSnapshot;
      workflowQueuedRef.current = false;
      if (needsFinalSync && consentRef.current && (phaseRef.current === "listening" || finishRequestedRef.current)) {
        window.setTimeout(() => runLiveWorkflow(transcriptRef.current, nameRef.current, finishRequestedRef.current), 0);
      } else if (finishRequestedRef.current && phaseRef.current === "reviewing") {
        finishRequestedRef.current = false;
        setPhase("saved");
        setStatusText("Saved live · People, evidence, and opportunities are updated");
      }
    }
  }

  const startVoice = async () => {
    if (!consent) return setError("Consent must be active before microphone access or voice processing.");
    setError("");
    setPhase("connecting");
    setStatusText("Requesting microphone permission…");
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      stream.getTracks().forEach((track) => track.stop());
      transcriptRef.current = "";
      nameRef.current = name.trim();
      personRef.current = null;
      conversationRef.current = null;
      lastSegmentRef.current = "";
      finishRequestedRef.current = false;
      workflowQueuedRef.current = false;
      setTranscript("");
      setPartialTranscript("");
      setName(name.trim());
      setVoiceMessages([]);
      setActivity([{ type: "voice.started", trace: { detail: "Live room opened; speech-to-text and LangGraph are standing by", mode: "Live room" } }]);
      const [voiceResponse, scribeResponse] = await Promise.all([
        fetch(apiPath("/api/voice/signed-url"), { headers: authHeaders(token) }),
        fetch(apiPath("/api/voice/scribe-token"), { method: "POST", headers: authHeaders(token, true), body: JSON.stringify({}) }),
      ]);
      const voiceBody = await voiceResponse.json();
      const scribeBody = await scribeResponse.json();
      if (!voiceResponse.ok) {
        if (voiceResponse.status === 401) throw new Error("Authentication required. Add the self-hosted bearer token in Settings.");
        throw new Error(voiceBody.reason || voiceBody.error || voiceBody.detail || `Voice service failed (${voiceResponse.status})`);
      }
      if (!voiceBody.signedUrl) throw new Error(voiceBody.reason || voiceBody.error || voiceBody.detail || "Voice agent is not configured");
      setPhase("listening");
      phaseRef.current = "listening";
      const started = await Promise.allSettled([
        startSession({ signedUrl: voiceBody.signedUrl, userId }),
        scribeResponse.ok && scribeBody.token
          ? scribe.connect({ token: scribeBody.token, modelId: scribeBody.modelId || "scribe_v2_realtime", microphone: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } })
          : Promise.reject(new Error(scribeBody.reason || "Realtime transcription token unavailable")),
      ]);
      if (started[0].status === "rejected") throw started[0].reason;
      appendActivity({ type: "agent.notice", trace: { detail: started[1].status === "fulfilled" ? "Realtime speech-to-text connected; the agent will process each meaningful turn" : "Voice agent connected; realtime Scribe is unavailable, so conversation transcripts will be used when delivered", mode: started[1].status === "fulfilled" ? "ElevenLabs Scribe" : "Voice fallback" } });
      setStatusText(started[1].status === "fulfilled" ? "Listening live · agent is ready to work in parallel" : "Listening live · transcript fallback active");
    } catch (caught) {
      try { await scribe.disconnect(); } catch (_) { /* the voice session may not have opened */ }
      setPhase("idle");
      phaseRef.current = "idle";
      setStatusText("Voice session could not start");
      setError(displayError(caught));
    }
  };

  const stopVoice = async () => {
    setPhase("reviewing");
    phaseRef.current = "reviewing";
    finishRequestedRef.current = true;
    setStatusText("Voice session ended · finishing the live agent run");
    try { await Promise.allSettled([endSession(), scribe.disconnect()]); } catch (caught) { setError(displayError(caught)); }
    if (!workflowInFlightRef.current) {
      if (nameRef.current && transcriptRef.current.trim()) await runLiveWorkflow(transcriptRef.current, nameRef.current, true);
      else {
        finishRequestedRef.current = false;
        setStatusText("Say the person’s name and a little context before finishing");
      }
    }
  };

  const openDraft = async (opportunity) => {
    setSelectedOpportunity(opportunity);
    setDraft(null);
    try {
      const response = await fetch(apiPath("/api/workflow"), {
        method: "POST", headers: authHeaders(token, true),
        body: JSON.stringify({ action: "draft", introduction: opportunity }),
      });
      const body = await response.json();
      if (!response.ok || !body.draft) throw new Error(body.error || body.reason || "Draft failed");
      setDraft(body.draft);
    } catch (caught) { setError(displayError(caught)); }
  };

  const saveToken = () => {
    localStorage.setItem(AUTH_KEY, tokenDraft.trim());
    setToken(tokenDraft.trim());
    setSettingsOpen(false);
  };

  const exportMemory = async () => {
    try {
      const response = await fetch(apiPath("/api/memory/export"), { headers: authHeaders(token) });
      if (!response.ok) throw new Error("Memory export failed");
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `secondhello-memory-${new Date().toISOString().slice(0, 10)}.json`;
      link.click();
      URL.revokeObjectURL(url);
    } catch (caught) { setError(displayError(caught)); }
  };

  const deleteMemory = async () => {
    if (!window.confirm("Delete every person, conversation, opportunity, and action from this self-hosted instance? This cannot be undone.")) return;
    try {
      const response = await fetch(apiPath("/api/memory"), { method: "DELETE", headers: { ...authHeaders(token), "X-SecondHello-Confirm": "DELETE_ALL" } });
      if (!response.ok) throw new Error("Memory deletion failed");
      setMemory({ people: [], conversations: [], actions: [] });
      setOpportunities([]); setActivity([]); setTranscript(""); setName(""); setSettingsOpen(false);
      setStatusText("All local relationship data was deleted");
    } catch (caught) { setError(displayError(caught)); }
  };

  const liveTranscript = partialTranscript ? `${transcript}${transcript ? "\n" : ""}${partialTranscript}` : transcript;
  const activityView = activity.length ? activity.map((event, index) => <div className={`activity-event ${index === activity.length - 1 ? "current" : ""}`} key={event.id}><span className="event-marker">{event.type === "workflow.failed" ? "!" : event.type === "workflow.completed" ? "✓" : "·"}</span><div><strong>{event.type === "workflow.completed" ? "Workflow complete" : event.type === "workflow.failed" ? "Workflow failed" : eventLabel(event)}</strong><small>{event.trace?.mode || (event.type === "workflow.completed" ? "Durable result" : "LangGraph event")}</small></div><time>{index === activity.length - 1 ? "now" : "done"}</time></div>) : <div className="empty-activity"><span>✦</span><div><strong>Agent activity will appear here</strong><p>Consent starts the live transcript, research, evidence, matching, and persistence loop.</p></div></div>;

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand"><span className="brand-mark">✦</span><div><strong>SECOND HELLO</strong><span>network memory, live</span></div></div>
        <div className="side-section-label">WORKSPACE</div>
        <button className="nav-item active"><span>◉</span> Live room <span className="nav-count">{people.length}</span></button>
        <button className="nav-item" onClick={() => document.getElementById("opportunities")?.scrollIntoView({ behavior: "smooth" })}><span>⌁</span> Opportunities <span className="nav-count">{opportunities.length}</span></button>
        <button className="nav-item" onClick={() => document.getElementById("people")?.scrollIntoView({ behavior: "smooth" })}><span>♧</span> People <span className="nav-count">{people.length}</span></button>
        <div className="sidebar-bottom">
          <div className="status-card"><span className={`status-dot ${health?.ok ? "online" : ""}`}></span><div><strong>{health?.ok ? "Agent online" : "Connecting…"}</strong><small>{storageLabel} · {modeLabel}</small></div></div>
          <button className="settings-button" onClick={() => setSettingsOpen(true)}>⚙ Settings & deployment</button>
        </div>
      </aside>

      <main className="main-canvas">
        <header className="topbar"><div><span className="eyebrow">CONSENT-FIRST NETWORKING AGENT</span><h1>Remember the room while you’re still in it.</h1><p>Consent once, then keep talking. Speech-to-text, public research, evidence, and matching run beside the live voice room as the conversation unfolds.</p></div><div className="top-actions"><span className={`pill ${consent ? "pill-green" : "pill-muted"}`}>{consent ? "● Consent armed" : "○ Consent off"}</span><button className="ghost-button" onClick={() => setSettingsOpen(true)}>Deploy locally ↗</button></div></header>

        {error && <div className="error-banner"><span>!</span><div><strong>Action needs attention</strong><p>{error}</p></div><button onClick={() => setError("")}>×</button></div>}

        <section className="live-grid">
          <div className="live-card card">
            <div className="card-kicker"><span className="live-badge"><i></i> LIVE ROOM</span><span>{voiceStatus === "connected" ? "ElevenLabs voice" : "Ready when you are"}</span></div>
            <div className={`orb-wrap ${phase === "listening" ? "is-live" : ""}`}><div className="orb-ring ring-one"></div><div className="orb-ring ring-two"></div><div className="orb"><span>{phase === "listening" ? icons.mic : phase === "processing" ? "…" : icons.spark}</span></div></div>
            <div className="live-copy"><h2>{phase === "listening" ? "Listening quietly" : phase === "processing" ? "Working through the graph" : phase === "saved" ? "Memory is live" : "Ready to remember"}</h2><p>{statusText}</p></div>
            <div className="live-controls">
              {phase === "listening" ? <><button className="primary-button stop" onClick={stopVoice}>{icons.stop} Stop & finish sync</button><button className="icon-button" onClick={() => setMuted(!isMuted)}>{isMuted ? "Unmute" : "Mute"}</button></> : <button className="primary-button" disabled={!consent || phase === "processing" || phase === "reviewing"} onClick={startVoice}>{icons.mic} {phase === "saved" ? "Start another live room" : phase === "reviewing" ? "Finishing live memory…" : "Start live room"}</button>}
            </div>
            <div className="consent-row"><span className={`consent-switch ${consent ? "on" : ""}`} onClick={() => setConsent(!consent)}><i></i></span><div><strong>{consent ? "Permission active" : "Permission required"}</strong><small>{consent ? "Mic, live transcript, and background agent are enabled." : "Nothing is recorded, extracted, researched, or stored."}</small></div><span className="lock">{icons.lock}</span></div>
          </div>

          <div className="capture-card card">
            <div className="card-header"><div><span className="eyebrow">LIVE MEMORY STREAM</span><h3>Who did you meet?</h3></div><span className="review-chip">{workflowBusy ? "Agent working" : phase === "saved" ? "Saved" : "Auto-save on"}</span></div>
            <div className="identity-fields"><label>NAME<input value={name} onChange={(event) => { nameRef.current = event.target.value; setName(event.target.value); }} placeholder="Say “I’m Ray” or enter a name" /></label><label>EMAIL <em>optional</em><input value={email} onChange={(event) => setEmail(event.target.value)} placeholder="For a future introduction draft" /></label></div>
            <div className="transcript-box"><div className="transcript-head"><span>LIVE SPEECH-TO-TEXT</span><span>{liveTranscript ? `${liveTranscript.split(/\s+/).filter(Boolean).length} words` : "Awaiting voice"}</span></div><textarea value={liveTranscript} onChange={(event) => { transcriptRef.current = event.target.value; setTranscript(event.target.value); setPartialTranscript(""); }} placeholder="Your speech appears here while you talk…" /><div className="transcript-foot"><span>{partialTranscript ? "Partial transcript · listening now" : workflowBusy ? "LangGraph is working in the background" : voiceMessages.length ? "Live speech captured" : "Only consented human speech enters memory"}</span><button onClick={() => { transcriptRef.current = ""; setTranscript(""); setPartialTranscript(""); }}>Clear</button></div></div>
            <div className="auto-save-state"><span>✦</span><div><strong>Saving continuously after consent</strong><small>Each meaningful turn updates the relationship graph while you remain in the room. Stop only when you want the final sync.</small></div></div>
          </div>

          <div className="agent-panel card"><div className="section-heading compact"><div><span className="eyebrow">LIVE AGENT ACTIVITY</span><h2>What is happening now</h2></div><span className={`agent-state ${workflowBusy ? "working" : ""}`}>{workflowBusy ? "WORKING" : phase === "listening" ? "LISTENING" : "READY"}</span></div><div className="activity-rail">{activityView}</div></div>
        </section>

        <section className="insights-grid" id="people"><div className="insight-panel card"><div className="section-heading compact"><div><span className="eyebrow">RELATIONSHIP GRAPH</span><h2>People in the room</h2></div><span className="number-badge">{people.length}</span></div>{people.length ? people.slice(-4).reverse().map((person) => <div className="person-row" key={person.id}><div className="avatar">{person.name?.slice(0, 1).toUpperCase()}</div><div><strong>{person.name}</strong><small>{person.email || "Professional context captured"}</small></div><span className="row-status">{person.id === latestPerson?.id ? "Just now" : "Remembered"}</span></div>) : <div className="empty-panel">Consent to a conversation and the person will appear here immediately.</div>}</div><div className="insight-panel card" id="opportunities"><div className="section-heading compact"><div><span className="eyebrow">EVIDENCE-BACKED MATCHES</span><h2>Connections worth making</h2></div><span className="number-badge accent">{opportunities.length}</span></div>{opportunities.length ? opportunities.slice(0, 3).map((opportunity) => <button className="opportunity-row" key={opportunity.id} onClick={() => openDraft(opportunity)}><div className="match-line"><span>{opportunity.recipientName}</span><b>↔</b><span>{opportunity.connectorName}</span><em>{Math.round((opportunity.score || 0) * 100)}%</em></div><p><strong>{opportunity.recipientName}</strong> needs {opportunity.need}; <strong>{opportunity.connectorName}</strong> offers {opportunity.offer}.</p><small>{opportunity.searchMode} · Review evidence {icons.arrow}</small></button>) : <div className="empty-panel">Research and matching results will land here after the workflow completes.</div>}</div></section>

        <footer className="footer"><span>Second Hello is local-first by design.</span><span>Consent receipt · Human approval · Nothing sent automatically</span></footer>
      </main>

      {settingsOpen && <div className="modal-backdrop" onClick={() => setSettingsOpen(false)}><div className="settings-modal" onClick={(event) => event.stopPropagation()}><button className="modal-close" onClick={() => setSettingsOpen(false)}>×</button><span className="eyebrow">SELF-HOSTED DEPLOYMENT</span><h2>Connect this client to your agent</h2><p>Run the Python service locally or behind your own TLS reverse proxy. The browser only receives a short-lived voice URL; provider keys stay on the server.</p><label>Bearer token<input type="password" value={tokenDraft} onChange={(event) => setTokenDraft(event.target.value)} placeholder="SECONDHELLO_AUTH_TOKEN (optional in development)" /></label><div className="settings-actions"><button className="ghost-button" onClick={() => { setTokenDraft(""); localStorage.removeItem(AUTH_KEY); setToken(""); }}>Clear token</button><button className="primary-button" onClick={saveToken}>Save & reconnect</button></div><div className="settings-note"><span>✓</span><small>API base: {API || "same origin"}<br />User identity: {userId.slice(0, 18)}…</small></div><div className="data-actions"><button className="ghost-button" onClick={exportMemory}>Export relationship data</button><button className="danger-button" onClick={deleteMemory}>Delete all data</button></div></div></div>}
      {draft && selectedOpportunity && <div className="modal-backdrop" onClick={() => setDraft(null)}><div className="draft-modal" onClick={(event) => event.stopPropagation()}><button className="modal-close" onClick={() => setDraft(null)}>×</button><span className="eyebrow">HUMAN-REVIEWED HANDOFF</span><h2>Connection note ready</h2><p>Second Hello prepared a draft for {selectedOpportunity.recipientName} and {selectedOpportunity.connectorName}. Nothing has been sent.</p><label>Subject<input value={draft.subject} onChange={(event) => setDraft({ ...draft, subject: event.target.value })} /></label><label>Body<textarea value={draft.body} onChange={(event) => setDraft({ ...draft, body: event.target.value })} /></label><div className="settings-actions"><button className="ghost-button" onClick={() => setDraft(null)}>Keep in tracker</button><button className="primary-button" onClick={() => { window.location.href = `mailto:${encodeURIComponent(draft.to || "")}?cc=${encodeURIComponent(draft.cc || "")}&subject=${encodeURIComponent(draft.subject)}&body=${encodeURIComponent(draft.body)}`; setDraft(null); }}>Open my mail app ↗</button></div></div></div>}
    </div>
  );
}

createRoot(document.getElementById("root")).render(<Provider><App /></Provider>);
