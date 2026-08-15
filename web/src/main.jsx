import React, { useCallback, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { useScribe } from "@elevenlabs/react";
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

const personNameStopwords = new Set(["a", "about", "an", "and", "at", "for", "from", "here", "i", "in", "is", "it", "just", "looking", "my", "of", "on", "or", "people", "seeking", "that", "the", "there", "this", "to", "trying", "who", "with", "working"]);

function isPersonName(value) {
  const normalized = String(value || "").trim().replace(/\s+/g, " ");
  if (!normalized || normalized.length > 100 || !/^\p{L}[\p{L}'’-]*(?:\s+\p{L}[\p{L}'’-]*){0,3}$/u.test(normalized)) return false;
  return !normalized.split(" ").some((word) => personNameStopwords.has(word.toLowerCase()));
}

function speakerName(transcript) {
  const normalizedTranscript = String(transcript || "")
    .replace(/[，、。！？；：]/g, (character) => ({ "，": ",", "、": ",", "。": ".", "！": "!", "？": "?", "；": ";", "：": ":" }[character] || character))
    .replace(/\s+/g, " ");
  const patterns = [
    /\b(?:my name is|this is)\s+([^.!?,;:\n]+)/i,
    /\b(?:i am|i[’']m|im)\s+([^.!?,;:\n]+)/i,
  ];
  for (const pattern of patterns) {
    const match = normalizedTranscript.match(pattern);
    if (!match) continue;
    const candidate = match[1].split(/\b(?:and|who|from|at|in|on|with|i|my)\b/i)[0].trim().split(/\s+/).slice(0, 4).join(" ");
    if (isPersonName(candidate)) return candidate;
  }
  return "";
}

function eventLabel(event) {
  const trace = event.trace;
  if (trace?.detail) return trace.detail;
  const labels = {
    guard: "Consent and input checks passed",
    extract: "Extracting explicit needs, offers, and topics",
    resolve_identity: "Collecting identity clues from the conversation",
    checkpoint_contact: "Saving the contact before optional research",
    plan_research: "Preparing a bounded public research query",
    research: "Researching public professional sources",
    verify_research: "Verifying identity and source evidence",
    persist: "Saving consented memory to the relationship graph",
    follow_up: "Preparing the follow-up tracker and connection note",
    match: "Searching for relevant connections",
    rank: "Ranking evidence-backed introductions",
    draft: "Preparing a human-reviewed connection note",
    record_action: "Recording the approved handoff",
  };
  return labels[event.node] || event.node || "Agent update";
}

function simpleActivity(event) {
  const trace = event.trace || {};
  const step = trace.tool || event.node || "";
  const subject = trace.subject || event.result?.person?.name || "the person";
  if (event.type === "workflow.started") return { title: "Agent started", detail: event.action === "capture" ? "Consent accepted; preparing the memory workflow" : "Preparing the requested action", type: event.type };
  if (event.type === "workflow.failed") return { title: "Needs attention", detail: "The background workflow stopped safely", type: event.type };
  if (event.type === "workflow.completed") {
    return { title: "Follow-up ready", detail: `Saved ${subject}, the discussion, and a connection note`, type: event.type };
  }
  if (event.type === "voice.started") return { title: "Listening to the conversation", detail: "Live transcript is active; nothing speaks back", type: event.type };
  if (event.type === "agent.notice") return { title: "Voice update", detail: trace.detail || "Live transcript is active; nothing speaks back", type: event.type };
  if (["consent_gate", "guard"].includes(step)) return { title: `Ready to remember ${subject}`, detail: "Consent and the person’s name were confirmed", type: event.type };
  if (["extract_memory", "extract"].includes(step)) return { title: `Understood ${subject}`, detail: `${trace.memoryCount || 0} explicit detail${trace.memoryCount === 1 ? "" : "s"} found in the conversation`, type: event.type };
  if (["resolve_identity", "identity"].includes(step)) return trace.identityStatus === "ready" ? { title: `Enough clues to search for ${subject}`, detail: `Using ${trace.clues?.join(" · ") || "spoken professional context"}`, type: event.type } : { title: `Learning who ${subject} is`, detail: "Contact will be saved now; waiting for company, role, or full name before search", type: event.type };
  if (["save_contact", "checkpoint_contact"].includes(step)) return { title: `Contact saved: ${subject}`, detail: "The conversation is durable even if public search remains uncertain", type: event.type };
  if (["plan_public_research", "plan_research"].includes(step)) return trace.cached ? { title: `Identity check already complete for ${subject}`, detail: "Reusing the completed decision instead of searching again", type: event.type } : trace.identityStatus === "collecting" ? { title: "Public search safely paused", detail: "More identity clues are needed; the contact is already saved", type: event.type } : { title: `Searching for ${subject}`, detail: `Using ${trace.queryTerms?.join(" · ") || "grounded conversation clues"}`, type: event.type };
  if (["web_research", "research"].includes(step)) return trace.cached ? { title: `Using the completed identity check`, detail: "No duplicate public search was started", type: event.type } : trace.skipped ? { title: `Not guessing about ${subject}`, detail: "Search skipped until the identity is specific enough", type: event.type } : trace.found ? { title: `Found public information for ${subject}`, detail: `${trace.sourceCount || 0} cited source${trace.sourceCount === 1 ? "" : "s"} returned`, type: event.type } : { title: `Search finished for ${subject}`, detail: "Results will be verified before anything is attached", type: event.type };
  if (["verify_sources", "verify_research"].includes(step)) return trace.cached ? { title: trace.found ? `${subject} remains verified` : `${subject} remains unverified`, detail: "The previous evidence-safe decision was preserved", type: event.type } : trace.found ? { title: `Verified ${subject}`, detail: `${trace.candidateCount || 0} cited research lead${trace.candidateCount === 1 ? "" : "s"} available`, type: event.type } : { title: `Kept ${subject} private`, detail: "Ambiguous research was not saved", type: event.type };
  if (["persist_memory", "persist"].includes(step)) return { title: `Saved ${subject}`, detail: trace.topics?.length ? `Contact and discussed details: ${trace.topics.join(" · ")}` : "Contact, transcript, and consent receipt saved", type: event.type };
  if (["prepare_follow_up", "follow_up"].includes(step)) return { title: `Follow-up ready for ${subject}`, detail: trace.discussed?.length ? `Connection note references: ${trace.discussed.join(" · ")}` : "Connection note uses only the conversation transcript", type: event.type };
  if (["find_introductions", "match", "rank_opportunities", "rank"].includes(step)) return { title: `Found ${trace.opportunityCount || 0} possible connection${trace.opportunityCount === 1 ? "" : "s"}`, detail: trace.opportunityCount ? `Matches for ${subject} use explicit needs and cited offers` : `No compatible stated offer from another saved contact yet`, type: event.type };
  if (step === "record_action") return { title: "Recording the handoff", detail: "Nothing is sent without your approval", type: event.type };
  return { title: "Agent update", detail: trace.detail || "A LangGraph step completed", type: event.type };
}

function Provider({ children }) {
  return children;
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
  const [memory, setMemory] = useState({ people: [], conversations: [], followUps: [], actions: [] });
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [consent, setConsent] = useState(false);
  const [transcript, setTranscript] = useState("");
  const [partialTranscript, setPartialTranscript] = useState("");
  const [activity, setActivity] = useState([]);
  const [research, setResearch] = useState(null);
  const [identityResolution, setIdentityResolution] = useState(null);
  const [phase, setPhase] = useState("idle");
  const [statusText, setStatusText] = useState("Arm consent to begin listening");
  const [error, setError] = useState("");
  const [selectedFollowUp, setSelectedFollowUp] = useState(null);
  const [draft, setDraft] = useState(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [tokenDraft, setTokenDraft] = useState(token);
  const [voiceMessages, setVoiceMessages] = useState([]);
  const [workflowBusy, setWorkflowBusy] = useState(false);
  const [lastCompletion, setLastCompletion] = useState(null);

  const transcriptRef = useRef("");
  const nameRef = useRef("");
  const phaseRef = useRef("idle");
  const consentRef = useRef(false);
  const personRef = useRef(null);
  const conversationRef = useRef(null);
  const workflowTimerRef = useRef(null);
  const workflowSnapshotRef = useRef({ text: "", name: "" });
  const workflowInFlightRef = useRef(false);
  const workflowQueuedRef = useRef(false);
  const finishRequestedRef = useRef(false);
  const lastSegmentRef = useRef("");

  useEffect(() => { transcriptRef.current = transcript; }, [transcript]);
  useEffect(() => { nameRef.current = name; }, [name]);
  useEffect(() => { phaseRef.current = phase; }, [phase]);
  useEffect(() => { consentRef.current = consent; }, [consent]);

  const scribe = useScribe({
    modelId: "scribe_v2_realtime",
    onPartialTranscript: (data) => {
      const partial = String(data?.text || "").trim();
      setPartialTranscript(partial);
      setStatusText("Live speech-to-text is updating");
      if (phaseRef.current === "listening" && partial) {
        const provisionalTranscript = transcriptRef.current ? `${transcriptRef.current}\n${partial}` : partial;
        const detectedName = nameRef.current || speakerName(provisionalTranscript);
        if (detectedName && !nameRef.current) {
          nameRef.current = detectedName;
          setName(detectedName);
        }
        if (detectedName) scheduleLiveWorkflow(provisionalTranscript, detectedName);
      }
    },
    onCommittedTranscript: (data) => handleTranscriptSegment(data?.text || ""),
    onError: (scribeError) => {
      appendActivity({ type: "agent.notice", trace: { detail: `Realtime transcription issue: ${displayError(scribeError)}`, mode: "ElevenLabs Scribe" } });
    },
  });
  const loadState = useCallback(async () => {
    setError("");
    try {
      const healthResponse = await fetch(apiPath("/api/health"), { headers: authHeaders(token) });
      const healthBody = await healthResponse.json();
      setHealth(healthBody);
      const memoryResponse = await fetch(apiPath("/api/memory"), { headers: authHeaders(token) });
      if (memoryResponse.ok) {
        const memoryBody = await memoryResponse.json();
        setMemory(memoryBody);
        if (!(memoryBody.people || []).length && !(memoryBody.conversations || []).length && phaseRef.current === "idle") {
          transcriptRef.current = "";
          nameRef.current = "";
          setName("");
          setEmail("");
          setTranscript("");
          setPartialTranscript("");
          setActivity([]);
          setResearch(null);
          setIdentityResolution(null);
        }
      }
    } catch (caught) {
      setError(`Agent server unavailable: ${displayError(caught)}`);
    }
  }, [token]);

  useEffect(() => { loadState(); }, [loadState]);

  const people = memory.people || [];
  const followUps = [...(memory.followUps || [])].sort((left, right) => String(right.createdAt || "").localeCompare(String(left.createdAt || "")));
  const orderedConversations = [...(memory.conversations || [])].sort((left, right) => String(left.timestamp || "").localeCompare(String(right.timestamp || "")));
  const currentConversation = orderedConversations.at(-1) || null;
  const latestPerson = people.find((person) => person.id === currentConversation?.personID) || people[people.length - 1];
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
    if (!nameSnapshot || !isPersonName(nameSnapshot)) {
      setStatusText("Listening · waiting for a clear introduction");
      return;
    }
    const enoughContext = textSnapshot.trim().length >= 24 || textSnapshot.trim().split(/\s+/).length >= 6;
    if (!enoughContext) return;
    workflowSnapshotRef.current = { text: textSnapshot, name: nameSnapshot };
    if (workflowTimerRef.current) return;
    workflowTimerRef.current = window.setTimeout(() => {
      workflowTimerRef.current = null;
      const snapshot = workflowSnapshotRef.current;
      runLiveWorkflow(snapshot.text, snapshot.name);
    }, 900);
  }

  function clearCompletedLiveSession(personName) {
    transcriptRef.current = "";
    nameRef.current = "";
    personRef.current = null;
    conversationRef.current = null;
    workflowSnapshotRef.current = { text: "", name: "" };
    lastSegmentRef.current = "";
    phaseRef.current = "saved";
    setName("");
    setEmail("");
    setTranscript("");
    setPartialTranscript("");
    setVoiceMessages([]);
    setIdentityResolution(null);
    setActivity([]);
    setLastCompletion({ personName, completedAt: new Date().toISOString() });
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
          if (event.identityResolution) setIdentityResolution(event.identityResolution);
          setStatusText(simpleActivity(event).title);
        }
        if (event.type === "workflow.completed") {
          result = event.result;
          if (!result?.ok) throw new Error(result?.reason || "Workflow rejected the capture");
          if (result.followUp) setMemory((current) => ({ ...current, followUps: [...(current.followUps || []).filter((item) => item.id !== result.followUp.id), result.followUp] }));
          if (result.identityResolution) setIdentityResolution(result.identityResolution);
        }
        if (event.type === "workflow.failed") throw new Error(event.message || "Workflow failed");
      }
      if (done) break;
    }
    return result;
  }

  async function runLiveWorkflow(textSnapshot, nameSnapshot, force = false) {
    if (!consentRef.current || !isPersonName(nameSnapshot) || !textSnapshot.trim()) return;
    if (!force && phaseRef.current !== "listening") return;
    if (workflowInFlightRef.current) {
      workflowQueuedRef.current = true;
      return;
    }
    workflowInFlightRef.current = true;
    setWorkflowBusy(true);
    setStatusText("Agent is processing this conversation in parallel…");
    let workflowSucceeded = false;
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
      workflowSucceeded = true;
      await loadState();
      if (finishRequestedRef.current && !workflowQueuedRef.current && transcriptRef.current === textSnapshot) {
        finishRequestedRef.current = false;
        setPhase("saved");
        setStatusText("Saved live · People, memory, and connections are updated");
        clearCompletedLiveSession(nameSnapshot);
      } else if (!finishRequestedRef.current) {
        setStatusText(result?.followUp ? `Follow-up ready for ${nameSnapshot} · still listening` : "Agent is listening for more context");
      }
    } catch (caught) {
      appendActivity({ type: "agent.notice", trace: { detail: `Live workflow paused: ${displayError(caught)}`, mode: "LangGraph" } });
      setStatusText("Background sync needs attention");
      setError(displayError(caught));
    } finally {
      workflowInFlightRef.current = false;
      setWorkflowBusy(false);
      const latestSnapshot = workflowSnapshotRef.current;
      const latestText = latestSnapshot.text || transcriptRef.current;
      const latestName = latestSnapshot.name || nameRef.current;
      const needsFinalSync = workflowQueuedRef.current || latestText !== textSnapshot;
      workflowQueuedRef.current = false;
      if (needsFinalSync && consentRef.current && (phaseRef.current === "listening" || finishRequestedRef.current)) {
        window.setTimeout(() => runLiveWorkflow(latestText, latestName, finishRequestedRef.current), 0);
      } else if (finishRequestedRef.current && phaseRef.current === "reviewing" && workflowSucceeded) {
        finishRequestedRef.current = false;
        setPhase("saved");
        setStatusText("Saved live · People, memory, and connections are updated");
        clearCompletedLiveSession(nameSnapshot);
      }
    }
  }

  const startVoice = async () => {
    if (!consent) return setError("Consent must be active before microphone access or voice processing.");
    if (name.trim() && !isPersonName(name)) return setError("Enter a person’s name, not a sentence or request.");
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
      workflowSnapshotRef.current = { text: "", name: "" };
      setTranscript("");
      setPartialTranscript("");
      setName(name.trim());
      setVoiceMessages([]);
      setIdentityResolution(null);
      setLastCompletion(null);
      setActivity([{ type: "voice.started", trace: { detail: "Live room opened; speech-to-text and LangGraph are standing by", mode: "Live room" } }]);
      const scribeResponse = await fetch(apiPath("/api/voice/scribe-token"), {
        method: "POST",
        headers: authHeaders(token, true),
        body: JSON.stringify({}),
      });
      const scribeBody = await scribeResponse.json();
      if (!scribeResponse.ok) {
        if (scribeResponse.status === 401) throw new Error("Authentication required. Add the self-hosted bearer token in Settings.");
        throw new Error(scribeBody.reason || scribeBody.error || scribeBody.detail || `Realtime transcription service failed (${scribeResponse.status})`);
      }
      if (!scribeBody.token) throw new Error(scribeBody.reason || scribeBody.error || scribeBody.detail || "Realtime transcription is not configured");
      setPhase("listening");
      phaseRef.current = "listening";
      await scribe.connect({
        token: scribeBody.token,
        modelId: scribeBody.modelId || "scribe_v2_realtime",
        microphone: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      appendActivity({ type: "agent.notice", trace: { detail: "Passive realtime speech-to-text connected; no voice response will interrupt the room", mode: "ElevenLabs Scribe" } });
      setStatusText("Listening live · passive transcript active");
    } catch (caught) {
      try { await scribe.disconnect(); } catch (_) { /* the Scribe session may not have opened */ }
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
    if (workflowTimerRef.current) {
      window.clearTimeout(workflowTimerRef.current);
      workflowTimerRef.current = null;
    }
    try { await scribe.disconnect(); } catch (caught) { setError(displayError(caught)); }
    if (!workflowInFlightRef.current) {
      if (nameRef.current && transcriptRef.current.trim()) await runLiveWorkflow(transcriptRef.current, nameRef.current, true);
      else {
        finishRequestedRef.current = false;
        setPhase("idle");
        phaseRef.current = "idle";
        setStatusText("No clear person name yet · start a new room and say “I’m Name”");
      }
    }
  };

  const openFollowUp = (followUp) => {
    setSelectedFollowUp(followUp);
    setDraft({ body: followUp.connectionNote || "" });
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
    if (!window.confirm("Delete every person, conversation, connection, and action from this self-hosted instance? This cannot be undone.")) return;
    try {
      const response = await fetch(apiPath("/api/memory"), { method: "DELETE", headers: { ...authHeaders(token), "X-SecondHello-Confirm": "DELETE_ALL" } });
      if (!response.ok) throw new Error("Memory deletion failed");
      setMemory({ people: [], conversations: [], followUps: [], actions: [] });
      setActivity([]); setTranscript(""); setName(""); setSettingsOpen(false);
      setStatusText("All local relationship data was deleted");
    } catch (caught) { setError(displayError(caught)); }
  };

  const liveTranscript = partialTranscript ? `${transcript}${transcript ? "\n" : ""}${partialTranscript}` : transcript;
  const compactActivity = activity.reduce((items, event) => {
    const summary = simpleActivity(event);
    if (items.at(-1)?.title === summary.title) items[items.length - 1] = { ...summary, id: event.id };
    else items.push({ ...summary, id: event.id });
    return items;
  }, []).slice(-7);
  const activityView = compactActivity.length ? compactActivity.map((item, index) => <div className={`activity-event ${index === compactActivity.length - 1 ? "current" : ""}`} key={item.id}><span className="event-marker">{item.type === "workflow.failed" ? "!" : item.type === "workflow.completed" ? "✓" : "·"}</span><div><strong>{item.title}</strong><small>{item.detail}</small></div><time>{index === compactActivity.length - 1 ? "now" : "done"}</time></div>) : lastCompletion ? <div className="completion-receipt"><span>✓</span><div><strong>Workflow completed</strong><p>{lastCompletion.personName} was saved and added to People to reconnect with.</p></div></div> : <div className="empty-activity"><span>✦</span><div><strong>Background work will appear here</strong><p>After consent, this shows the current step in plain language.</p></div></div>;
  const activeIdentity = identityResolution;
  const identityStatus = activeIdentity?.status || "waiting";
  const identityLabels = {
    waiting: ["WAITING", "Say the person’s name naturally. The contact will be saved before any public search."],
    collecting: ["CONTACT SAVED · COLLECTING CLUES", activeIdentity?.message || "Listening for a company, role, or full name."],
    ready: ["READY TO SEARCH", "Enough grounded context has been collected for a safe public search."],
    searching: ["SEARCHING PUBLIC SOURCES", activeIdentity?.message || "Looking for one identity supported by multiple clues."],
    verified: ["IDENTITY VERIFIED", activeIdentity?.message || "A cited public professional identity was attached."],
    ambiguous: ["CONTACT SAVED · IDENTITY UNVERIFIED", activeIdentity?.message || "Results were ambiguous, so no profile was attached."],
  };
  const [identityTitle, identityMessage] = identityLabels[identityStatus] || identityLabels.waiting;

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand"><span className="brand-mark">✦</span><div><strong>SECOND HELLO</strong><span>network memory, live</span></div></div>
        <div className="side-section-label">WORKSPACE</div>
        <button className="nav-item active"><span>◉</span> Live room <span className="nav-count">{people.length}</span></button>
        <button className="nav-item" onClick={() => document.getElementById("connections")?.scrollIntoView({ behavior: "smooth" })}><span>⌁</span> Follow-ups <span className="nav-count">{followUps.length}</span></button>
        <button className="nav-item" onClick={() => document.getElementById("people")?.scrollIntoView({ behavior: "smooth" })}><span>♧</span> People <span className="nav-count">{people.length}</span></button>
        <div className="sidebar-bottom">
          <div className="status-card"><span className={`status-dot ${health?.ok ? "online" : ""}`}></span><div><strong>{health?.ok ? "Agent online" : "Connecting…"}</strong><small>{health?.storageFallback ? `${storageLabel} fallback` : storageLabel} · {modeLabel}</small></div></div>
          <button className="settings-button" onClick={() => setSettingsOpen(true)}>⚙ Settings & deployment</button>
        </div>
      </aside>

      <main className="main-canvas">
        <header className="topbar"><div><span className="eyebrow">YOUR PRIVATE NETWORK MEMORY ASSISTANT</span><h1>Remember the room while you’re still in it.</h1><p>Consent once, then keep talking. I’ll remember who you met, what you discussed, and which connections are worth following up on.</p></div><div className="top-actions"><span className={`pill ${consent ? "pill-green" : "pill-muted"}`}>{consent ? "● Consent armed" : "○ Consent off"}</span><button className="ghost-button" onClick={() => setSettingsOpen(true)}>Deploy locally ↗</button></div></header>

        {error && <div className="error-banner"><span>!</span><div><strong>Action needs attention</strong><p>{error}</p></div><button onClick={() => setError("")}>×</button></div>}

        <section className="live-grid">
          <div className="live-card card">
            <div className="card-kicker"><span className="live-badge"><i></i> LIVE ROOM</span><span>{phase === "listening" ? "Passive listener · Scribe realtime" : "Ready when you are"}</span></div>
            <div className={`orb-wrap ${phase === "listening" ? "is-live" : ""}`}><div className="orb-ring ring-one"></div><div className="orb-ring ring-two"></div><div className="orb"><span>{phase === "listening" ? icons.mic : phase === "processing" ? "…" : icons.spark}</span></div></div>
            <div className="live-copy"><h2>{phase === "listening" ? "Listening quietly" : phase === "processing" ? "Working through the graph" : phase === "saved" ? "Memory is live" : "Ready to remember"}</h2><p>{statusText}</p></div>
            <div className="live-controls">
              {phase === "listening" ? <><button className="primary-button stop" onClick={stopVoice}>{icons.stop} Stop & finish sync</button><button className="icon-button" onClick={() => scribe.isMuted ? scribe.unmute() : scribe.mute()}>{scribe.isMuted ? "Resume mic" : "Pause mic"}</button></> : <button className="primary-button" disabled={!consent || phase === "processing" || phase === "reviewing"} onClick={startVoice}>{icons.mic} {phase === "saved" ? "Start another live room" : phase === "reviewing" ? "Finishing live memory…" : "Start live room"}</button>}
            </div>
            <div className="consent-row"><button className={`consent-switch ${consent ? "on" : ""}`} aria-pressed={consent} aria-label={consent ? "Disable consent" : "Enable consent"} onClick={() => setConsent(!consent)}><i></i></button><div><strong>{consent ? "Permission active" : "Permission required"}</strong><small>{consent ? "Mic, live transcript, and background agent are enabled." : "Nothing is recorded, extracted, researched, or stored."}</small></div><span className="lock">{icons.lock}</span></div>
          </div>

          <div className="capture-card card">
            <div className="card-header"><div><span className="eyebrow">LIVE MEMORY STREAM</span><h3>Who did you meet?</h3></div><span className="review-chip">{workflowBusy ? "Agent working" : phase === "saved" ? "Saved" : "Auto-save on"}</span></div>
            <div className="identity-fields"><label>NAME<input value={name} onChange={(event) => { nameRef.current = event.target.value; setName(event.target.value); }} placeholder="Say “I’m Ray” or enter a name" /></label><label>EMAIL <em>optional</em><input value={email} onChange={(event) => setEmail(event.target.value)} placeholder="For a future introduction draft" /></label></div>
            <div className={`identity-resolution ${identityStatus}`}><div className="identity-resolution-head"><span>IDENTITY RESOLUTION</span><strong>{identityTitle}</strong></div><p>{identityMessage}</p><div className="identity-clues">{activeIdentity?.clues?.length ? activeIdentity.clues.map((clue) => <span key={`${clue.kind}-${clue.value}`}><b>{clue.label}</b>{clue.value}</span>) : <span className="empty-clue">No identity clues collected yet</span>}</div><div className="identity-progress"><span className={activeIdentity ? "done" : "active"}>1 · Save contact</span><span className={["ready", "searching", "verified"].includes(identityStatus) ? "done" : identityStatus === "collecting" ? "active" : ""}>2 · Collect clues</span><span className={identityStatus === "verified" ? "done" : identityStatus === "searching" ? "active" : ""}>3 · Verify profile</span></div></div>
            <div className="transcript-box"><div className="transcript-head"><span>LIVE SPEECH-TO-TEXT</span><span>{liveTranscript ? `${liveTranscript.split(/\s+/).filter(Boolean).length} words` : "Awaiting voice"}</span></div><textarea value={liveTranscript} onChange={(event) => { transcriptRef.current = event.target.value; setTranscript(event.target.value); setPartialTranscript(""); }} placeholder="Your speech appears here while you talk…" /><div className="transcript-foot"><span>{partialTranscript ? "Partial transcript · listening now" : workflowBusy ? "LangGraph is working in the background" : voiceMessages.length ? "Live speech captured" : "Only consented human speech enters memory"}</span><button onClick={() => { transcriptRef.current = ""; setTranscript(""); setPartialTranscript(""); }}>Clear</button></div></div>
            <div className="auto-save-state"><span>✦</span><div><strong>Saving continuously after consent</strong><small>Each meaningful turn updates the relationship graph while you remain in the room. Stop only when you want the final sync.</small></div></div>
          </div>

          <div className="agent-panel card"><div className="section-heading compact"><div><span className="eyebrow">BACKGROUND WORK</span><h2>What the agent is doing</h2></div><span className={`agent-state ${workflowBusy || phase === "reviewing" ? "working" : ""}`}>{phase === "reviewing" ? "FINALIZING" : workflowBusy ? "WORKING" : phase === "listening" ? "LISTENING" : lastCompletion ? "COMPLETED" : "READY"}</span></div><div className="activity-rail">{activityView}</div></div>
        </section>

        <section className="insights-grid" id="people"><div className="insight-panel card"><div className="section-heading compact"><div><span className="eyebrow">RELATIONSHIP MEMORY</span><h2>People you met</h2></div><span className="number-badge">{people.length}</span></div>{people.length ? people.slice(-4).reverse().map((person) => <div className="person-row" key={person.id}><div className="avatar">{person.name?.slice(0, 1).toUpperCase()}</div><div><strong>{person.name}</strong><small>{person.email || "Conversation captured"}</small></div><span className="row-status">{person.id === latestPerson?.id ? "Just now" : "Remembered"}</span></div>) : <div className="empty-panel">Consent to a conversation and the person will appear here automatically.</div>}</div><div className="insight-panel card" id="connections"><div className="section-heading compact"><div><span className="eyebrow">YOUR FOLLOW-UP QUEUE</span><h2>People to reconnect with</h2></div><span className="number-badge accent">{followUps.length}</span></div>{followUps.length ? followUps.slice(0, 4).map((followUp) => <button className="opportunity-row" key={followUp.id} onClick={() => openFollowUp(followUp)}><div className="match-line"><span>{followUp.personName}</span><em>{followUp.status === "new" ? "FOLLOW UP" : String(followUp.status || "saved").toUpperCase()}</em></div><p>{followUp.summary || "Conversation saved for follow-up."}</p><small>{followUp.nextStep || "Review connection note"} {icons.arrow}</small></button>) : <div className="empty-panel">Every consented conversation becomes a follow-up here—no special phrasing required.</div>}</div></section>

        <footer className="footer"><span>Second Hello is local-first by design.</span><span>Consent receipt · Human approval · Nothing sent automatically</span></footer>
      </main>

      {settingsOpen && <div className="modal-backdrop" onClick={() => setSettingsOpen(false)}><div className="settings-modal" onClick={(event) => event.stopPropagation()}><button className="modal-close" onClick={() => setSettingsOpen(false)}>×</button><span className="eyebrow">SELF-HOSTED DEPLOYMENT</span><h2>Connect this client to your agent</h2><p>Run the Python service locally or behind your own TLS reverse proxy. The browser receives only a short-lived realtime transcription token; provider keys stay on the server.</p><label>Bearer token<input type="password" value={tokenDraft} onChange={(event) => setTokenDraft(event.target.value)} placeholder="SECONDHELLO_AUTH_TOKEN (optional in development)" /></label><div className="settings-actions"><button className="ghost-button" onClick={() => { setTokenDraft(""); localStorage.removeItem(AUTH_KEY); setToken(""); }}>Clear token</button><button className="primary-button" onClick={saveToken}>Save & reconnect</button></div><div className="settings-note"><span>✓</span><small>API base: {API || "same origin"}<br />User identity: {userId.slice(0, 18)}…</small></div><div className="data-actions"><button className="ghost-button" onClick={exportMemory}>Export relationship data</button><button className="danger-button" onClick={deleteMemory}>Delete all data</button></div></div></div>}
      {draft && selectedFollowUp && <div className="modal-backdrop" onClick={() => setDraft(null)}><div className="draft-modal" onClick={(event) => event.stopPropagation()}><button className="modal-close" onClick={() => setDraft(null)}>×</button><span className="eyebrow">FOLLOW-UP READY</span><h2>Reconnect with {selectedFollowUp.personName}</h2><p>{selectedFollowUp.summary} Nothing has been sent.</p><label>Editable connection note<textarea value={draft.body} onChange={(event) => setDraft({ ...draft, body: event.target.value })} /></label><div className="settings-actions"><button className="ghost-button" onClick={async () => { await navigator.clipboard.writeText(draft.body); setStatusText("Connection note copied"); }}>Copy note</button><button className="primary-button" onClick={() => window.open(selectedFollowUp.profileURL, "_blank", "noopener,noreferrer")}>Find on LinkedIn ↗</button></div></div></div>}
    </div>
  );
}

createRoot(document.getElementById("root")).render(<Provider><App /></Provider>);
