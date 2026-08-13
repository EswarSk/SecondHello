import Foundation

struct Person: Codable, Identifiable, Hashable {
    var id: UUID
    var name: String
    var email: String?
    var createdAt: Date

    init(id: UUID, name: String, email: String? = nil, createdAt: Date) {
        self.id = id; self.name = name; self.email = email; self.createdAt = createdAt
    }
}

struct Evidence: Codable, Identifiable, Hashable {
    var id: UUID = UUID()
    var quote: String
    var conversationID: UUID
    var capturedAt: Date
}

struct Profile: Codable, Hashable {
    var needs: [String] = []
    var offers: [String] = []
    var topics: [String] = []
    var commitments: [String] = []
    var evidence: [Evidence] = []
}

struct Conversation: Codable, Identifiable, Hashable {
    var id: UUID = UUID()
    var personID: UUID
    var timestamp: Date = .now
    var consented: Bool
    var consentedAt: Date?
    var transcript: String
    var profile: Profile

    init(id: UUID = UUID(), personID: UUID, timestamp: Date = .now, consented: Bool, consentedAt: Date? = nil, transcript: String, profile: Profile) {
        self.id = id; self.personID = personID; self.timestamp = timestamp; self.consented = consented; self.consentedAt = consentedAt; self.transcript = transcript; self.profile = profile
    }
}

struct StoredMemory: Codable {
    var schemaVersion = 1
    var people: [Person] = []
    var conversations: [Conversation] = []
}

struct Introduction: Identifiable, Hashable {
    var id = UUID()
    var recipient: Person
    var connector: Person
    var complementaryNeed: String
    var complementaryOffer: String
    var needEvidence: Evidence
    var offerEvidence: Evidence
    var score: Double
    var searchMode: String
}

struct ToolTrace: Codable, Identifiable, Hashable {
    var id: UUID
    var tool: String
    var detail: String
    var mode: String
    var completedAt: String
}

struct IntroductionDraft: Codable, Hashable {
    var to: String
    var cc: String
    var subject: String
    var body: String
}

struct ServerEvidence: Codable, Hashable { var quote: String }

struct ServerOpportunity: Codable, Identifiable, Hashable {
    var id: UUID
    var recipientID: UUID
    var recipientName: String
    var recipientEmail: String?
    var connectorID: UUID
    var connectorName: String
    var connectorEmail: String?
    var need: String
    var offer: String
    var score: Double
    var needEvidence: ServerEvidence
    var offerEvidence: ServerEvidence
    var searchMode: String
}

struct DemoScenario: Codable {
    struct Guest: Codable, Identifiable {
        var id: String { email }
        var name: String
        var email: String
        var transcript: String
    }
    var event: String
    var guests: [Guest]

    static func load() -> DemoScenario? {
        guard let url = Bundle.module.url(forResource: "demo_scenario", withExtension: "json"),
              let data = try? Data(contentsOf: url) else { return nil }
        return try? JSONDecoder().decode(DemoScenario.self, from: data)
    }
}
