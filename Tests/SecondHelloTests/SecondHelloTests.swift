import XCTest
@testable import SecondHello

final class SecondHelloTests: XCTestCase {
    func testExtractorFindsFixtureNeedAndOffer() throws {
        let guest = try XCTUnwrap(DemoScenario.load()?.guests.first)
        let profile = LocalExtractor.extract(guest.transcript, conversationID: UUID())
        XCTAssertTrue(profile.needs.contains { $0.localizedCaseInsensitiveContains("technical cofounder") })
        XCTAssertTrue(profile.offers.contains { $0.localizedCaseInsensitiveContains("founder introductions") })
    }

    func testJSONRepositoryPersists() throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let repository = JSONMemoryRepository(url: url)
        let memory = StoredMemory(people: [Person(id: UUID(), name: "Test", createdAt: .now)])
        try repository.save(memory)
        XCTAssertEqual(try repository.load().people.first?.name, "Test")
        try? FileManager.default.removeItem(at: url)
    }

    @MainActor
    func testConsentGatePreventsLocalMutation() async {
        let store = MemoryStore(repository: JSONMemoryRepository(url: FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)))
        let saved = await store.capture(name: "Private Person", email: "", transcript: "I need advice.", consented: false)
        XCTAssertFalse(saved)
        XCTAssertTrue(store.memory.people.isEmpty)
        XCTAssertTrue(store.memory.conversations.isEmpty)
    }

    @MainActor
    func testScenarioProducesEvidenceBackedIntroductionWithoutSpecialCase() async throws {
        UserDefaults.standard.removeObject(forKey: "serverURL")
        let scenario = try XCTUnwrap(DemoScenario.load())
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let store = MemoryStore(repository: JSONMemoryRepository(url: url))
        for guest in scenario.guests {
            let captured = await store.capture(name: guest.name, email: guest.email, transcript: guest.transcript, consented: true)
            XCTAssertTrue(captured)
        }
        let match = try XCTUnwrap(store.introductions().first)
        XCTAssertEqual(match.recipient.name, scenario.guests[0].name)
        XCTAssertEqual(match.connector.name, scenario.guests[1].name)
        XCTAssertTrue(match.needEvidence.quote.localizedCaseInsensitiveContains("technical cofounder"))
        XCTAssertEqual(match.searchMode, "Local semantic")
        try? FileManager.default.removeItem(at: url)
    }

    func testMailActionCreatesDraftURLWithoutSending() throws {
        let draft = IntroductionDraft(to: "first@example.com", cc: "second@example.com", subject: "An introduction", body: "Review me")
        let url = try XCTUnwrap(MailDraftOpener.url(for: draft))
        XCTAssertEqual(url.scheme, "mailto")
        XCTAssertTrue(url.absoluteString.contains("subject="))
    }

    func testElevenLabsPCMFormatParser() {
        XCTAssertEqual(ElevenLabsAudioFormat.sampleRate(from: "pcm_16000"), 16_000)
        XCTAssertEqual(ElevenLabsAudioFormat.sampleRate(from: "pcm_24000"), 24_000)
        XCTAssertNil(ElevenLabsAudioFormat.sampleRate(from: "ulaw_8000"))
    }

    @MainActor
    func testVoiceAgentStartsWithoutCredentialsOrMicrophoneSideEffects() {
        let agent = ElevenLabsConversationService()
        XCTAssertFalse(agent.isActive)
        XCTAssertTrue(agent.transcript.isEmpty)
        XCTAssertEqual(agent.statusText, "Ready for a private voice conversation")
        agent.reset()
        XCTAssertFalse(agent.isActive)
    }

    @MainActor
    func testLiveListenerStartsPrivateAndCanResetWithoutOpeningMicrophone() {
        let listener = LiveListeningService()
        XCTAssertFalse(listener.isListening)
        XCTAssertTrue(listener.transcript.isEmpty)
        listener.transcript = "Unsaved words"
        listener.reset()
        XCTAssertTrue(listener.transcript.isEmpty)
        XCTAssertEqual(listener.statusText, "Ready when permission is granted")
    }
}
