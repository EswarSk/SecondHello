import Foundation

protocol MemoryRepository {
    func load() throws -> StoredMemory
    func save(_ memory: StoredMemory) throws
}

/// The offline-first cache. Server persistence is additive and never makes the
/// native demo dependent on credentials or venue connectivity.
struct JSONMemoryRepository: MemoryRepository {
    let url: URL
    init(fileManager: FileManager = .default) {
        let base = fileManager.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
            .appendingPathComponent("SecondHello", isDirectory: true)
        try? fileManager.createDirectory(at: base, withIntermediateDirectories: true)
        url = base.appendingPathComponent("memory.json")
    }
    init(url: URL) { self.url = url }
    func load() throws -> StoredMemory {
        guard FileManager.default.fileExists(atPath: url.path) else { return StoredMemory() }
        return try JSONDecoder.secondHello.decode(StoredMemory.self, from: Data(contentsOf: url))
    }
    func save(_ memory: StoredMemory) throws {
        let data = try JSONEncoder.secondHello.encode(memory)
        try data.write(to: url, options: .atomic)
    }
}

extension JSONDecoder {
    static var secondHello: JSONDecoder { let decoder = JSONDecoder(); decoder.dateDecodingStrategy = .iso8601; return decoder }
}
extension JSONEncoder {
    static var secondHello: JSONEncoder { let encoder = JSONEncoder(); encoder.dateEncodingStrategy = .iso8601; encoder.outputFormatting = [.prettyPrinted, .sortedKeys]; return encoder }
}

@MainActor
final class MemoryStore: ObservableObject {
    @Published private(set) var memory = StoredMemory()
    @Published private(set) var remoteOpportunities: [Introduction] = []
    @Published var lastError: String?
    @Published var workflowStatus = "Offline Demo Mode"
    @Published var workflowDetail = "Local extraction · local memory · no network"
    @Published var voiceAgentConfigured = false
    @Published var traces: [ToolTrace] = []
    @Published var isWorking = false
    private let repository: any MemoryRepository

    init(repository: any MemoryRepository = JSONMemoryRepository()) {
        self.repository = repository
        reload()
        Task {
            await checkServer()
            await refreshOpportunities()
        }
    }

    func reload() {
        do { memory = try repository.load() } catch { lastError = error.localizedDescription }
    }

    func checkServer() async {
        do {
            let health = try await WorkflowClient.health()
            workflowStatus = health.storage == "MongoDB Atlas" ? "Atlas Agent Online" : "Local Agent Online"
            workflowDetail = "\(health.workflow) · \(health.storage) · \(health.provider)" + (health.vectorSearch ? " · Vector Search" : "")
            voiceAgentConfigured = health.voiceAgentConfigured ?? false
        } catch {
            workflowStatus = "Offline Demo Mode"
            workflowDetail = "Local extraction · local memory · no network"
            voiceAgentConfigured = false
        }
    }

    private func person(named name: String, email: String?) -> Person {
        if let index = memory.people.firstIndex(where: { $0.name.localizedCaseInsensitiveCompare(name) == .orderedSame }) {
            if let email, !email.isEmpty { memory.people[index].email = email }
            return memory.people[index]
        }
        let person = Person(id: UUID(), name: name.trimmingCharacters(in: .whitespacesAndNewlines), email: email?.nilIfEmpty, createdAt: .now)
        memory.people.append(person)
        return person
    }

    @discardableResult
    func capture(name: String, email: String, transcript: String, consented: Bool) async -> Bool {
        guard consented else { lastError = "Explicit consent is required before extraction or storage."; return false }
        isWorking = true; lastError = nil; traces = []
        let person = person(named: name, email: email)
        let conversationID = UUID()
        let local = LocalExtractor.extract(transcript, conversationID: conversationID)
        var profile = local
        var serverOpportunities: [ServerOpportunity]?
        let shell = Conversation(id: conversationID, personID: person.id, consented: true, consentedAt: .now, transcript: transcript, profile: Profile())

        // A consented person should appear in People immediately. Public
        // research can continue without making the successful save look lost.
        let localConversation = Conversation(id: conversationID, personID: person.id, consented: true, consentedAt: .now, transcript: transcript, profile: local)
        memory.conversations.append(localConversation)
        persist()
        do {
            let result = try await WorkflowClient.run(action: "capture", values: ["person": try WorkflowClient.jsonObject(person), "conversation": try WorkflowClient.jsonObject(shell)])
            guard result.ok else { throw NSError(domain: "SecondHello", code: 1, userInfo: [NSLocalizedDescriptionKey: result.reason ?? "Workflow rejected capture"]) }
            profile = result.profile ?? local
            serverOpportunities = result.opportunities
            traces = result.trace ?? []
            workflowStatus = "Agent completed"
            workflowDetail = traces.map(\.mode).uniqued().joined(separator: " · ")
        } catch {
            let completedAt = ISO8601DateFormatter().string(from: .now)
            traces = [
                ToolTrace(id: UUID(), tool: "consent_gate", detail: "Permission and input checks passed", mode: "Local policy", completedAt: completedAt),
                ToolTrace(id: UUID(), tool: "extract_memory", detail: "Extracted explicit needs, offers, topics, and commitments", mode: "Local deterministic", completedAt: completedAt),
                ToolTrace(id: UUID(), tool: "persist_memory", detail: "Stored consent receipt, evidence, and structured memory", mode: "Local JSON", completedAt: completedAt),
                ToolTrace(id: UUID(), tool: "find_introductions", detail: "Compared consented needs and offers", mode: "Local semantic", completedAt: completedAt)
            ]
            workflowStatus = "Offline Demo Mode"
            workflowDetail = "Local extraction · local memory · no network"
        }
        let conversation = Conversation(id: conversationID, personID: person.id, consented: true, consentedAt: .now, transcript: transcript, profile: profile)
        memory.conversations.removeAll { $0.id == conversationID }
        memory.conversations.append(conversation)
        persist()
        if let serverOpportunities {
            remoteOpportunities = serverOpportunities.compactMap(mapOpportunity)
            await checkServer()
        } else {
            remoteOpportunities = localIntroductions()
        }
        isWorking = false
        return true
    }

    func profile(for person: Person) -> Profile {
        memory.conversations.filter { $0.personID == person.id }.reduce(Profile()) { partial, conversation in
            Profile(
                needs: (partial.needs + conversation.profile.needs).uniqued(),
                offers: (partial.offers + conversation.profile.offers).uniqued(),
                topics: (partial.topics + conversation.profile.topics).uniqued(),
                commitments: (partial.commitments + conversation.profile.commitments).uniqued(),
                evidence: partial.evidence + conversation.profile.evidence,
                publicSummary: conversation.profile.publicSummary ?? partial.publicSummary,
                publicRoles: ((partial.publicRoles ?? []) + (conversation.profile.publicRoles ?? [])).uniqued(),
                publicOffers: ((partial.publicOffers ?? []) + (conversation.profile.publicOffers ?? [])).uniqued(),
                researchEvidence: (partial.researchEvidence ?? []) + (conversation.profile.researchEvidence ?? [])
            )
        }
    }

    func introductions() -> [Introduction] {
        remoteOpportunities.isEmpty ? localIntroductions() : remoteOpportunities
    }

    private func localIntroductions() -> [Introduction] {
        var results: [Introduction] = []
        for recipient in memory.people {
            let recipientProfile = profile(for: recipient)
            for connector in memory.people where connector.id != recipient.id {
                let connectorProfile = profile(for: connector)
                for need in recipientProfile.needs {
                    let ranked = connectorProfile.offers.map { ($0, similarity(need, $0)) }.max { $0.1 < $1.1 }
                    guard let (offer, score) = ranked, score >= 0.18,
                          let needEvidence = evidence(for: need, in: recipientProfile),
                          let offerEvidence = evidence(for: offer, in: connectorProfile) else { continue }
                    results.append(Introduction(recipient: recipient, connector: connector, complementaryNeed: need, complementaryOffer: offer, needEvidence: needEvidence, offerEvidence: offerEvidence, score: score, searchMode: "Local semantic"))
                }
            }
        }
        var best: [String: Introduction] = [:]
        for result in results {
            let key = "\(result.recipient.id)-\(result.connector.id)"
            if best[key] == nil || result.score > best[key]!.score { best[key] = result }
        }
        return best.values.sorted { $0.score > $1.score }
    }

    private func similarity(_ left: String, _ right: String) -> Double {
        let ignored = Set(["the", "and", "for", "with", "that", "this", "from", "into", "our", "can", "need", "offer"])
        func words(_ value: String) -> Set<String> { Set(value.lowercased().split(whereSeparator: { !$0.isLetter && !$0.isNumber }).map(String.init).filter { $0.count > 2 && !ignored.contains($0) }) }
        let a = words(left), b = words(right)
        guard !a.isEmpty, !b.isEmpty else { return 0 }
        return Double(a.intersection(b).count) / Double(min(a.count, b.count))
    }

    private func evidence(for value: String, in profile: Profile) -> Evidence? {
        let words = value.lowercased().split(whereSeparator: { !$0.isLetter }).filter { $0.count > 2 }
        return profile.evidence.max { left, right in
            words.filter { left.quote.lowercased().contains($0) }.count < words.filter { right.quote.lowercased().contains($0) }.count
        }
    }

    func refreshOpportunities() async {
        guard WorkflowClient.baseURL != nil else { remoteOpportunities = []; return }
        do {
            try await mergeServerMemory()
            let result = try await WorkflowClient.run(action: "match")
            traces = result.trace ?? traces
            remoteOpportunities = (result.opportunities ?? []).compactMap(mapOpportunity)
            await checkServer()
        } catch { remoteOpportunities = [] }
    }

    /// Atlas is authoritative when connected, while UUID-based merging keeps
    /// unsynced offline records intact for the next successful live session.
    private func mergeServerMemory() async throws {
        let remote = try await WorkflowClient.memory()
        for person in remote.people {
            if let index = memory.people.firstIndex(where: { $0.id == person.id }) { memory.people[index] = person }
            else { memory.people.append(person) }
        }
        for conversation in remote.conversations {
            if let index = memory.conversations.firstIndex(where: { $0.id == conversation.id }) { memory.conversations[index] = conversation }
            else { memory.conversations.append(conversation) }
        }
        memory.schemaVersion = max(memory.schemaVersion, remote.schemaVersion)
        persist()
    }

    private func mapOpportunity(_ value: ServerOpportunity) -> Introduction? {
        guard let recipient = memory.people.first(where: { $0.id == value.recipientID }) else { return nil }
        let connector = memory.people.first(where: { $0.id == value.connectorID }) ?? Person(id: value.connectorID, name: value.connectorName, email: value.connectorEmail, createdAt: .now)
        let recipientProfile = profile(for: recipient), connectorProfile = profile(for: connector)
        let needEvidence = evidence(for: value.need, in: recipientProfile) ?? evidence(from: value.needEvidence)
        let offerEvidence = value.offerEvidence.sourceURL == nil ? (evidence(for: value.offer, in: connectorProfile) ?? evidence(from: value.offerEvidence)) : evidence(from: value.offerEvidence)
        return Introduction(id: value.id, recipient: recipient, connector: connector, complementaryNeed: value.need, complementaryOffer: value.offer, needEvidence: needEvidence, offerEvidence: offerEvidence, score: value.score, searchMode: value.searchMode)
    }

    private func evidence(from value: ServerEvidence) -> Evidence {
        let captured = value.capturedAt.flatMap { ISO8601DateFormatter().date(from: $0) } ?? .now
        return Evidence(quote: value.quote, conversationID: value.conversationID.flatMap(UUID.init(uuidString:)) ?? UUID(), capturedAt: captured, sourceURL: value.sourceURL, sourceTitle: value.sourceTitle)
    }

    func draft(for idea: Introduction) async -> IntroductionDraft {
        let local = IntroductionDraft(to: idea.recipient.email ?? "", cc: idea.connector.email ?? "", subject: "Intro: \(idea.recipient.name) × \(idea.connector.name)", body: "Hi \(idea.recipient.name) and \(idea.connector.name),\n\nYou both explicitly said you were open to relevant introductions. \(idea.recipient.name) is looking for \(idea.complementaryNeed); \(idea.connector.name) can offer \(idea.complementaryOffer).\n\nWould you like to connect? I’ll leave it to you both from here.\n")
        let payload: [String: Any] = ["id": idea.id.uuidString, "recipientID": idea.recipient.id.uuidString, "recipientName": idea.recipient.name, "recipientEmail": idea.recipient.email ?? "", "connectorID": idea.connector.id.uuidString, "connectorName": idea.connector.name, "connectorEmail": idea.connector.email ?? "", "need": idea.complementaryNeed, "offer": idea.complementaryOffer, "score": idea.score, "searchMode": idea.searchMode]
        do {
            let result = try await WorkflowClient.run(action: "draft", values: ["introduction": payload])
            traces = result.trace ?? traces
            return result.draft ?? local
        } catch { return local }
    }

    func recordMailHandoff(for idea: Introduction) async {
        let receipt: [String: Any] = ["id": UUID().uuidString, "kind": "mail_draft_opened", "introductionID": idea.id.uuidString, "recipientIDs": [idea.recipient.id.uuidString, idea.connector.id.uuidString], "createdAt": ISO8601DateFormatter().string(from: .now)]
        if let result = try? await WorkflowClient.run(action: "record_action", values: ["action_receipt": receipt]) { traces = result.trace ?? traces }
    }

    func clear() { memory = StoredMemory(); remoteOpportunities = []; traces = []; persist() }
    private func persist() { do { try repository.save(memory) } catch { lastError = error.localizedDescription } }
}

private extension String {
    var nilIfEmpty: String? { trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? nil : self }
}
private extension Array where Element: Hashable {
    func uniqued() -> [Element] { var seen = Set<Element>(); return filter { seen.insert($0).inserted } }
}
