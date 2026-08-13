import SwiftUI
import UniformTypeIdentifiers

@main
struct SecondHelloApp: App {
    @StateObject private var store = MemoryStore()
    var body: some Scene {
        WindowGroup { ContentView().environmentObject(store).frame(minWidth: 1080, minHeight: 720) }
            .windowStyle(.hiddenTitleBar)
    }
}

enum AppSection: Int { case today, capture, opportunities, people, trust }

struct ContentView: View {
    @EnvironmentObject private var store: MemoryStore
    @State private var selection = AppSection.today.rawValue
    @StateObject private var listener = LiveListeningService()
    @StateObject private var voiceAgent = ElevenLabsConversationService()
    @State private var captureName = ""
    @State private var captureEmail = ""
    @State private var captureConsent = false
    @State private var captureImporting = false
    @State private var captureStatus = ""

    private func detectedName(in transcript: String) -> String? {
        let lower = transcript.lowercased()
        for lead in ["my name is ", "i'm ", "i am "] {
            guard let range = lower.range(of: lead) else { continue }
            let offset = lower.distance(from: lower.startIndex, to: range.upperBound)
            let start = transcript.index(transcript.startIndex, offsetBy: offset)
            let tail = transcript[start...]
            let candidate = tail.split(whereSeparator: { ",.!?;\n".contains($0) }).first.map(String.init) ?? ""
            let words = candidate.split(separator: " ").prefix(3).map(String.init)
            guard !words.isEmpty else { continue }
            let rejected = ["a", "an", "looking", "interested", "working", "seeking"]
            guard !rejected.contains(words[0].lowercased()) else { continue }
            return words.joined(separator: " ")
        }
        return nil
    }

    private func finishCaptureAndOpenOpportunities() {
        guard !store.isWorking else { return }
        captureStatus = "Finalizing the transcript…"
        Task { @MainActor in
            // Let ElevenLabs deliver the final utterance before closing its socket.
            try? await Task.sleep(for: .milliseconds(900))
            let liveTranscript = voiceAgent.transcript.trimmingCharacters(in: .whitespacesAndNewlines)
            let fallbackTranscript = listener.transcript.trimmingCharacters(in: .whitespacesAndNewlines)
            let transcript = liveTranscript.isEmpty ? fallbackTranscript : liveTranscript
            listener.transcript = transcript
            if captureName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                captureName = detectedName(in: transcript) ?? ""
            }
            listener.stop(); voiceAgent.stop()

            let finalName = captureName.trimmingCharacters(in: .whitespacesAndNewlines)
            guard captureConsent else {
                captureStatus = "Listening stopped because consent is no longer active."
                return
            }
            guard !finalName.isEmpty else {
                captureStatus = "Say your name during the conversation or enter it before finishing."
                selection = AppSection.capture.rawValue
                return
            }
            guard !transcript.isEmpty else {
                captureStatus = "No speech was captured. Start listening and try again."
                selection = AppSection.capture.rawValue
                return
            }

            captureStatus = "Saving consented memory to Atlas and finding opportunities…"
            if await store.capture(name: finalName, email: captureEmail, transcript: transcript, consented: true) {
                captureStatus = "Memory saved. Opportunities are ready."
                selection = AppSection.opportunities.rawValue
            } else {
                captureStatus = store.lastError ?? "The memory workflow could not complete."
                selection = AppSection.capture.rawValue
            }
        }
    }
    var body: some View {
        NavigationSplitView {
            VStack(alignment: .leading, spacing: 20) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("SECOND HELLO").font(.headline).tracking(1.4).foregroundStyle(.mint)
                    Text("Turn permission into connection").font(.caption).foregroundStyle(.secondary)
                }
                List(selection: $selection) {
                    Label("Tonight", systemImage: "sparkles").tag(AppSection.today.rawValue)
                    Label("Remember", systemImage: "waveform.badge.mic").tag(AppSection.capture.rawValue)
                    Label("Opportunities", systemImage: "point.3.connected.trianglepath.dotted").tag(AppSection.opportunities.rawValue)
                    Label("People", systemImage: "person.2").tag(AppSection.people.rawValue)
                    Label("Trust center", systemImage: "checkmark.shield").tag(AppSection.trust.rawValue)
                }.listStyle(.sidebar)
                Spacer()
                if listener.isListening || voiceAgent.isActive {
                    VStack(alignment: .leading, spacing: 8) {
                        Label("Background listening active", systemImage: "waveform")
                            .font(.caption.bold()).foregroundStyle(.green)
                        Button(store.isWorking ? "Finding opportunities…" : "Stop & find opportunities") {
                            finishCaptureAndOpenOpportunities()
                        }.buttonStyle(.bordered).disabled(store.isWorking)
                    }
                }
                VStack(alignment: .leading, spacing: 4) {
                    Label(store.workflowStatus, systemImage: store.workflowStatus.contains("Offline") ? "wifi.slash" : "bolt.horizontal.circle.fill")
                        .font(.caption.bold()).foregroundStyle(store.workflowStatus.contains("Offline") ? Color.secondary : Color.green)
                    Text(store.workflowDetail).font(.caption2).foregroundStyle(.tertiary).lineLimit(2)
                }
            }.padding()
        } detail: {
            switch AppSection(rawValue: selection) ?? .today {
            case .today: TodayView(openCapture: { selection = AppSection.capture.rawValue }, openOpportunities: { selection = AppSection.opportunities.rawValue })
            case .capture: CaptureView(
                onSaved: { selection = AppSection.opportunities.rawValue },
                name: $captureName,
                email: $captureEmail,
                consent: $captureConsent,
                importing: $captureImporting,
                status: $captureStatus,
                listener: listener,
                voiceAgent: voiceAgent,
                onFinish: finishCaptureAndOpenOpportunities
            )
            case .opportunities: OpportunitiesView()
            case .people: PeopleView()
            case .trust: TrustCenterView()
            }
        }
    }
}

struct TodayView: View {
    @EnvironmentObject private var store: MemoryStore
    let openCapture: () -> Void
    let openOpportunities: () -> Void
    var body: some View {
        let ideas = store.introductions()
        ScrollView {
            VStack(alignment: .leading, spacing: 26) {
                VStack(alignment: .leading, spacing: 8) {
                    Text("The room was full.\nYour memory shouldn’t be.").font(.system(size: 40, weight: .bold, design: .rounded))
                    Text("Second Hello remembers only what people permitted—and finds the connection you would otherwise miss.")
                        .font(.title3).foregroundStyle(.secondary).frame(maxWidth: 720, alignment: .leading)
                }
                HStack(spacing: 14) {
                    MetricCard(value: "\(store.memory.people.count)", label: "people remembered", icon: "person.crop.circle.badge.checkmark")
                    MetricCard(value: "\(ideas.count)", label: "introductions worth making", icon: "sparkles")
                    MetricCard(value: "0", label: "messages sent without you", icon: "hand.raised.fill")
                }
                if let idea = ideas.first {
                    GroupBox {
                        HStack(spacing: 20) {
                            ZStack { Circle().fill(.mint.opacity(0.15)).frame(width: 64, height: 64); Image(systemName: "point.3.filled.connected.trianglepath.dotted").font(.title).foregroundStyle(.mint) }
                            VStack(alignment: .leading, spacing: 7) {
                                Text("A connection is hiding in your conversations").font(.caption.bold()).foregroundStyle(.mint).textCase(.uppercase)
                                Text("\(idea.recipient.name) × \(idea.connector.name)").font(.title2.bold())
                                Text("One needs \(idea.complementaryNeed). The other offered \(idea.complementaryOffer).")
                                    .foregroundStyle(.secondary).lineLimit(2)
                            }
                            Spacer()
                            Button("See the evidence") { openOpportunities() }.buttonStyle(.borderedProminent).controlSize(.large)
                        }.padding(8)
                    }
                } else {
                    GroupBox {
                        HStack(spacing: 18) {
                            Image(systemName: "person.2.wave.2").font(.system(size: 34)).foregroundStyle(.mint)
                            VStack(alignment: .leading, spacing: 4) {
                                Text("Start with two conversations").font(.title3.bold())
                                Text(store.workflowStatus.contains("Offline") ? "Use the bundled event scenario or capture consented notes." : "Capture two real, consented conversations to reveal a valuable connection.")
                                    .foregroundStyle(.secondary)
                            }
                            Spacer(); Button("Remember someone") { openCapture() }.buttonStyle(.borderedProminent).controlSize(.large)
                        }.padding(8)
                    }
                }
                if !store.traces.isEmpty { ToolTraceRail(traces: store.traces) }
            }.padding(38)
        }
    }
}

struct MetricCard: View {
    let value: String; let label: String; let icon: String
    var body: some View {
        GroupBox { HStack(spacing: 12) { Image(systemName: icon).font(.title2).foregroundStyle(.mint); VStack(alignment: .leading) { Text(value).font(.title.bold()); Text(label).font(.caption).foregroundStyle(.secondary) } }.frame(maxWidth: .infinity, alignment: .leading).padding(4) }.frame(maxWidth: .infinity)
    }
}

struct CaptureView: View {
    @EnvironmentObject private var store: MemoryStore
    let onSaved: () -> Void
    @Binding var name: String
    @Binding var email: String
    @Binding var consent: Bool
    @Binding var importing: Bool
    @Binding var status: String
    @ObservedObject var listener: LiveListeningService
    @ObservedObject var voiceAgent: ElevenLabsConversationService
    let onFinish: () -> Void
    private let scenario = DemoScenario.load()
    private var isCapturing: Bool { listener.isListening || voiceAgent.isActive }
    private var activeLevel: Double { voiceAgent.isActive ? voiceAgent.audioLevel : listener.audioLevel }
    private var activeStatus: String { voiceAgent.isActive || voiceAgent.phase != .idle ? voiceAgent.statusText : listener.statusText }
    private var activeEngine: String { voiceAgent.phase != .idle ? voiceAgent.engineLabel : listener.engineLabel }

    private func stopCapture() {
        listener.stop(); voiceAgent.stop()
    }

    private func resetCapture() {
        listener.reset(); voiceAgent.reset()
    }

    private func startCapture() {
        Task {
            do {
                voiceAgent.transcript = listener.transcript
                try await voiceAgent.start()
                status = "Consent confirmed. Quiet background capture is active; say “Second Hello” when you want help."
            } catch {
                voiceAgent.stop()
                await listener.start()
                status = listener.isListening
                    ? "ElevenLabs unavailable; continuing with reliable Apple Speech fallback."
                    : error.localizedDescription
            }
        }
    }

    private func detectedName(in transcript: String) -> String? {
        let lower = transcript.lowercased()
        for lead in ["my name is ", "i'm ", "i am "] {
            guard let range = lower.range(of: lead) else { continue }
            let offset = lower.distance(from: lower.startIndex, to: range.upperBound)
            let start = transcript.index(transcript.startIndex, offsetBy: offset)
            let tail = transcript[start...]
            let candidate = tail.split(whereSeparator: { ",.!?;\n".contains($0) }).first.map(String.init) ?? ""
            let words = candidate.split(separator: " ").prefix(3).map(String.init)
            guard !words.isEmpty else { continue }
            let rejected = ["a", "an", "looking", "interested", "working", "seeking"]
            guard !rejected.contains(words[0].lowercased()) else { continue }
            return words.joined(separator: " ")
        }
        return nil
    }
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Remember the moment").font(.largeTitle.bold())
                    Text("Consent is a workflow gate—not fine print. Until it is granted, no extraction, provider call, or storage occurs.").foregroundStyle(.secondary)
                }
                if store.workflowStatus.contains("Offline"), let scenario {
                    HStack {
                        Label("Demo event: \(scenario.event)", systemImage: "ticket").font(.subheadline.bold())
                        Spacer()
                        ForEach(scenario.guests) { guest in
                            Button("Load \(guest.name.components(separatedBy: " ").first ?? guest.name)") { resetCapture(); name = guest.name; email = guest.email; listener.transcript = guest.transcript; consent = false; status = "Demo conversation loaded. Consent is intentionally still off." }.buttonStyle(.bordered)
                        }
                    }.padding(12).background(.mint.opacity(0.08), in: RoundedRectangle(cornerRadius: 12))
                }
                HStack { TextField("Person’s name", text: $name); TextField("Email for a future draft (optional)", text: $email) }.textFieldStyle(.roundedBorder)
                HStack(spacing: 14) {
                    Toggle(isOn: $consent) { VStack(alignment: .leading) { Text("They explicitly agreed to live listening").fontWeight(.semibold); Text("Unlocks the microphone. Stopping publishes only this consented memory and its matched opportunities.").font(.caption).foregroundStyle(.secondary) } }.toggleStyle(.switch)
                    Spacer()
                    Label(consent ? "Permission active" : "Microphone locked", systemImage: consent ? "checkmark.shield.fill" : "lock.fill").font(.caption.bold()).foregroundStyle(consent ? Color.green : Color.secondary)
                }.padding(14).background(consent ? .green.opacity(0.09) : .secondary.opacity(0.07), in: RoundedRectangle(cornerRadius: 12))
                HStack(spacing: 24) {
                    if voiceAgent.isActive && !voiceAgent.isAgentSpeaking {
                        ZStack {
                            Circle().fill(.green.opacity(0.14)).frame(width: 54, height: 54)
                            Circle().fill(.green).frame(width: 12, height: 12).shadow(color: .green.opacity(0.6), radius: 7)
                        }.accessibilityLabel("Background microphone active")
                    } else {
                        VoiceOrb(level: activeLevel, listening: isCapturing)
                    }
                    VStack(alignment: .leading, spacing: 8) {
                        Text(voiceAgent.isAgentSpeaking ? "Second Hello is speaking" : (voiceAgent.isActive ? "Background capture active" : (isCapturing ? "Listening now" : "Live conversation capture"))).font(.title3.bold())
                        Text(activeStatus).foregroundStyle(.secondary)
                        Text(activeEngine).font(.caption).foregroundStyle(.tertiary)
                        HStack {
                            if isCapturing {
                                Button(store.isWorking ? "Finding opportunities…" : "Stop & find opportunities") { onFinish() }
                                    .buttonStyle(.borderedProminent).controlSize(.large).disabled(store.isWorking)
                            } else {
                                Button("Start background capture") { startCapture() }.buttonStyle(.borderedProminent).controlSize(.large).disabled(!consent)
                            }
                            Button("Import recording…") { importing = true }.buttonStyle(.bordered).disabled(!consent || isCapturing)
                        }
                    }
                    Spacer()
                    Label("Nothing saved yet", systemImage: "internaldrive").font(.caption).foregroundStyle(.secondary)
                }.padding(18).background(.mint.opacity(isCapturing ? 0.12 : 0.05), in: RoundedRectangle(cornerRadius: 16)).overlay(RoundedRectangle(cornerRadius: 16).stroke(isCapturing ? .mint.opacity(0.5) : .clear))
                if !voiceAgent.agentResponse.isEmpty && (voiceAgent.isAgentSpeaking || !voiceAgent.isActive) {
                    HStack(alignment: .top, spacing: 10) {
                        Image(systemName: "waveform.circle.fill").font(.title2).foregroundStyle(.mint)
                        VStack(alignment: .leading, spacing: 3) {
                            Text("SECOND HELLO").font(.caption2.bold()).tracking(1).foregroundStyle(.secondary)
                            Text(voiceAgent.agentResponse).textSelection(.enabled)
                        }
                    }.padding(12).background(.mint.opacity(0.07), in: RoundedRectangle(cornerRadius: 12))
                }
                VStack(alignment: .leading, spacing: 6) {
                    HStack { Text("REVIEWABLE HUMAN TRANSCRIPT").font(.caption.bold()).tracking(1).foregroundStyle(.secondary); Spacer(); if !listener.transcript.isEmpty { Button("Clear") { resetCapture() }.buttonStyle(.plain).foregroundStyle(.secondary) } }
                    TextEditor(text: $listener.transcript).font(.body).frame(minHeight: 150).scrollContentBackground(.hidden).padding(8).background(.background.opacity(0.7), in: RoundedRectangle(cornerRadius: 12)).overlay(RoundedRectangle(cornerRadius: 12).stroke(.quaternary))
                }
                HStack(spacing: 14) {
                    Label("Reviewable memory", systemImage: "eye.fill").font(.caption).foregroundStyle(.secondary)
                    Text("Saving records the consent receipt, transcript, and extracted memory.").font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    Button(store.isWorking ? "Running tools…" : "Remember with permission") {
                        stopCapture()
                        Task { if await store.capture(name: name, email: email, transcript: listener.transcript, consented: consent) { status = "Memory saved. Looking for a meaningful connection…"; onSaved() } }
                    }.buttonStyle(.borderedProminent).controlSize(.large).disabled(!consent || isCapturing || name.trimmingCharacters(in: .whitespaces).isEmpty || listener.transcript.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || store.isWorking)
                }
                if !status.isEmpty { Label(status, systemImage: consent ? "checkmark.seal.fill" : "lock.fill").foregroundStyle(consent ? .green : .secondary) }
            }.padding(38)
        }.onChange(of: consent) { _, enabled in
            if enabled && status.contains("intentionally still off") { status = "Consent confirmed. Workflow tools are now unlocked." }
            if !enabled && isCapturing { stopCapture(); status = "Listening stopped immediately because permission was withdrawn." }
        }.onChange(of: voiceAgent.transcript) { _, value in
            if !value.isEmpty {
                listener.transcript = value
                if name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                   let detected = detectedName(in: value) {
                    name = detected
                    status = "Name detected from the live conversation. Continue speaking, then stop and review."
                }
            }
        }.fileImporter(isPresented: $importing, allowedContentTypes: [.audio, .movie]) { result in
            switch result {
            case .success(let url):
                status = "Transcribing with the configured private provider…"
                Task { let granted = url.startAccessingSecurityScopedResource(); defer { if granted { url.stopAccessingSecurityScopedResource() } }; do { listener.transcript = try await ElevenLabsTranscriber.transcribe(fileURL: url); status = "Transcript ready. Review before remembering." } catch { status = error.localizedDescription } }
            case .failure(let error): status = error.localizedDescription
            }
        }
    }
}

struct VoiceOrb: View {
    let level: Double
    let listening: Bool
    var body: some View {
        ZStack {
            Circle().fill(.mint.opacity(listening ? 0.12 : 0.06)).frame(width: 112, height: 112).scaleEffect(1 + level * 0.16)
            Circle().stroke(.mint.opacity(listening ? 0.45 : 0.18), lineWidth: 2).frame(width: 88, height: 88).scaleEffect(1 + level * 0.1)
            Circle().fill(listening ? .mint : .secondary.opacity(0.16)).frame(width: 68, height: 68)
            Image(systemName: listening ? "waveform" : "mic.fill").font(.system(size: 27, weight: .semibold)).foregroundStyle(listening ? .black.opacity(0.72) : .secondary)
        }.animation(.easeOut(duration: 0.12), value: level).accessibilityLabel(listening ? "Microphone listening" : "Microphone stopped")
    }
}

struct OpportunitiesView: View {
    @EnvironmentObject private var store: MemoryStore
    @State private var evidence: Introduction?
    @State private var drafting: Introduction?
    var body: some View {
        let ideas = store.introductions()
        VStack(alignment: .leading, spacing: 18) {
            HStack(alignment: .bottom) {
                VStack(alignment: .leading, spacing: 5) { Text("Connections worth making").font(.largeTitle.bold()); Text("Ranked from consented needs and offers. Every claim opens back to its source.").foregroundStyle(.secondary) }
                Spacer(); Button { Task { await store.refreshOpportunities() } } label: { Label("Refresh", systemImage: "arrow.clockwise") }.buttonStyle(.bordered)
            }
            if ideas.isEmpty {
                ContentUnavailableView("No connection yet", systemImage: "point.3.connected.trianglepath.dotted", description: Text("Remember two conversations with complementary needs and offers."))
            } else {
                List(ideas) { idea in
                    VStack(alignment: .leading, spacing: 12) {
                        HStack {
                            VStack(alignment: .leading, spacing: 4) { Text("\(idea.recipient.name) × \(idea.connector.name)").font(.title3.bold()); Label("\(Int(idea.score * 100))% semantic fit · \(idea.searchMode)", systemImage: "scope").font(.caption).foregroundStyle(.mint) }
                            Spacer(); Button("Show evidence") { evidence = idea }.buttonStyle(.bordered); Button("Prepare introduction") { drafting = idea }.buttonStyle(.borderedProminent)
                        }
                        HStack(alignment: .top, spacing: 16) {
                            EvidencePreview(label: "NEEDS", person: idea.recipient.name, value: idea.complementaryNeed)
                            Image(systemName: "arrow.left.arrow.right").foregroundStyle(.tertiary).padding(.top, 22)
                            EvidencePreview(label: "OFFERS", person: idea.connector.name, value: idea.complementaryOffer)
                        }
                    }.padding(.vertical, 10)
                }.listStyle(.inset)
            }
            if !store.traces.isEmpty { ToolTraceRail(traces: store.traces) }
        }.padding(38).sheet(item: $evidence) { WhySheet(idea: $0) }.sheet(item: $drafting) { DraftSheet(idea: $0) }
    }
}

struct EvidencePreview: View {
    let label: String; let person: String; let value: String
    var body: some View { VStack(alignment: .leading, spacing: 3) { Text(label).font(.caption2.bold()).foregroundStyle(.secondary); Text(person).font(.caption.bold()); Text(value).lineLimit(2) }.frame(maxWidth: .infinity, alignment: .leading).padding(10).background(.secondary.opacity(0.06), in: RoundedRectangle(cornerRadius: 8)) }
}

struct ToolTraceRail: View {
    let traces: [ToolTrace]
    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack { Text("LIVE AGENT RECEIPT").font(.caption.bold()).tracking(1).foregroundStyle(.secondary); Spacer(); Label("auditable", systemImage: "checkmark.seal").font(.caption).foregroundStyle(.green) }
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    ForEach(Array(traces.enumerated()), id: \.element.id) { index, item in
                        if index > 0 { Image(systemName: "chevron.right").font(.caption2).foregroundStyle(.tertiary) }
                        VStack(alignment: .leading, spacing: 2) { Label(item.tool.replacingOccurrences(of: "_", with: " "), systemImage: "checkmark.circle.fill").font(.caption.bold()).foregroundStyle(.green); Text(item.detail).font(.caption2).foregroundStyle(.secondary).lineLimit(1); Text(item.mode).font(.caption2).foregroundStyle(.tertiary) }.padding(9).background(.green.opacity(0.07), in: RoundedRectangle(cornerRadius: 9))
                    }
                }
            }
        }.padding(12).background(.secondary.opacity(0.04), in: RoundedRectangle(cornerRadius: 12))
    }
}

struct PeopleView: View {
    @EnvironmentObject private var store: MemoryStore
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("People, not leads").font(.largeTitle.bold())
            Text("A compact memory assembled only from explicitly permitted conversations.").foregroundStyle(.secondary)
            List(store.memory.people) { person in
                let profile = store.profile(for: person)
                VStack(alignment: .leading, spacing: 9) {
                    HStack { Text(person.name).font(.title3.bold()); if let email = person.email { Text(email).font(.caption).foregroundStyle(.secondary) }; Spacer(); Label("Consented", systemImage: "checkmark.shield.fill").font(.caption).foregroundStyle(.green) }
                    if !profile.needs.isEmpty { LabeledContent("Needs", value: profile.needs.joined(separator: " · ")) }
                    if !profile.offers.isEmpty { LabeledContent("Offers", value: profile.offers.joined(separator: " · ")) }
                    if !profile.commitments.isEmpty { LabeledContent("Commitments", value: profile.commitments.joined(separator: " · ")) }
                    Text("\(profile.evidence.count) source excerpts retained for explainability").font(.caption).foregroundStyle(.secondary)
                }.padding(.vertical, 8)
            }.listStyle(.inset)
        }.padding(38)
    }
}

struct WhySheet: View {
    let idea: Introduction
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack { VStack(alignment: .leading) { Text("Why these two?").font(.title.bold()); Text("Evidence, not an AI hunch").foregroundStyle(.secondary) }; Spacer(); Text("\(Int(idea.score * 100))% fit").font(.headline).foregroundStyle(.mint) }
            GroupBox("\(idea.recipient.name) · stated need") { Text("“\(idea.needEvidence.quote)”").frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 4) }
            GroupBox("\(idea.connector.name) · stated offer") { Text("“\(idea.offerEvidence.quote)”").frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 4) }
            Label("\(idea.searchMode) ranked the match. No external action has been taken.", systemImage: "lock.fill").foregroundStyle(.green)
            HStack { Spacer(); Button("Close") { dismiss() }.keyboardShortcut(.defaultAction) }
        }.padding(28).frame(width: 620)
    }
}

struct DraftSheet: View {
    let idea: Introduction
    @EnvironmentObject private var store: MemoryStore
    @Environment(\.dismiss) private var dismiss
    @State private var draft: IntroductionDraft?
    @State private var reviewed = false
    @State private var status = "Generating an evidence-grounded draft…"
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("You stay in control").font(.title.bold())
            Text("Second Hello prepares the handoff. Your default mail app—not the agent—owns sending.").foregroundStyle(.secondary)
            if let draft {
                TextField("To", text: Binding(get: { draft.to }, set: { self.draft?.to = $0 })).textFieldStyle(.roundedBorder)
                TextField("Cc", text: Binding(get: { draft.cc }, set: { self.draft?.cc = $0 })).textFieldStyle(.roundedBorder)
                TextField("Subject", text: Binding(get: { draft.subject }, set: { self.draft?.subject = $0 })).textFieldStyle(.roundedBorder)
                TextEditor(text: Binding(get: { draft.body }, set: { self.draft?.body = $0 })).frame(height: 210).padding(6).overlay(RoundedRectangle(cornerRadius: 8).stroke(.quaternary))
                Toggle("I reviewed this draft and want to open it in Mail", isOn: $reviewed)
                HStack { Label("Nothing has been sent", systemImage: "hand.raised.fill").foregroundStyle(.green); Spacer(); Button("Cancel") { dismiss() }; Button("Open in Mail app") { if MailDraftOpener.open(draft) { status = "Draft handed to your mail app. You still choose whether to send."; Task { await store.recordMailHandoff(for: idea) } } }.buttonStyle(.borderedProminent).disabled(!reviewed) }
            } else { ProgressView(status).frame(maxWidth: .infinity, minHeight: 260) }
            Text(status).font(.caption).foregroundStyle(.secondary)
        }.padding(28).frame(width: 680).task { draft = await store.draft(for: idea); status = "Draft ready for your review. Nothing has been sent." }
    }
}

struct TrustCenterView: View {
    @EnvironmentObject private var store: MemoryStore
    @State private var serverURL = UserDefaults.standard.string(forKey: "serverURL") ?? ""
    @State private var elevenLabsKey = Keychain.read(account: "elevenlabs-api-key") ?? ""
    @State private var saved = false
    var body: some View {
        Form {
            Section("Live architecture") {
                LabeledContent("Status", value: store.workflowStatus)
                Text(store.workflowDetail).foregroundStyle(.secondary)
                TextField("Workflow server URL, e.g. http://127.0.0.1:8765", text: $serverURL)
                Button("Save and test connection") { UserDefaults.standard.set(serverURL.trimmingCharacters(in: .whitespacesAndNewlines), forKey: "serverURL"); Task { await store.checkServer() } }
            }
            Section("Realtime voice") {
                LabeledContent("Private ElevenLabs agent", value: store.voiceAgentConfigured ? "Ready" : "Not configured")
                Label(store.voiceAgentConfigured ? "The native app receives only a short-lived signed URL." : "Add ELEVENLABS_API_KEY to the local server’s ignored .env file, then restart the server.", systemImage: store.voiceAgentConfigured ? "lock.shield.fill" : "key.horizontal")
                    .foregroundStyle(store.voiceAgentConfigured ? .green : .secondary)
                Text("Agent ID is already configured. The API key never enters the macOS voice session.").font(.caption).foregroundStyle(.secondary)
                DisclosureGroup("Optional legacy file transcription and spoken briefings") {
                    SecureField("ElevenLabs API key", text: $elevenLabsKey)
                    Button("Save legacy key in macOS Keychain") { Keychain.save(elevenLabsKey, account: "elevenlabs-api-key"); saved = true }
                    if saved { Label("Stored in Keychain", systemImage: "key.fill").foregroundStyle(.green) }
                }
            }
            Section("Consent contract") {
                Label("No extraction or persistence before consent", systemImage: "checkmark.circle.fill")
                Label("No introduction without source evidence", systemImage: "checkmark.circle.fill")
                Label("No message is ever sent automatically", systemImage: "checkmark.circle.fill")
            }
            Section("Demo controls") { Button("Clear local demo memory", role: .destructive) { store.clear() } }
        }.formStyle(.grouped).padding().navigationTitle("Trust center")
    }
}
