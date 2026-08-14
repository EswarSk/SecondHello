import Foundation
import AppKit
@preconcurrency import AVFoundation
import Security
@preconcurrency import Speech

struct WorkflowResponse: Codable {
    var ok: Bool
    var reason: String?
    var profile: Profile?
    var conversation: Conversation?
    var opportunities: [ServerOpportunity]?
    var draft: IntroductionDraft?
    var trace: [ToolTrace]?
}

struct ServerHealth: Codable {
    var ok: Bool
    var workflow: String
    var storage: String
    var provider: String
    var vectorSearch: Bool
    var voiceAgentConfigured: Bool?
    var safeFallback: Bool
}

struct SignedConversation: Codable {
    var ok: Bool
    var signedUrl: String?
    var reason: String?
}

/// Optional local-server bridge. Any transport/configuration error is surfaced
/// to the caller so the app can continue with its deterministic local logic.
enum WorkflowClient {
    static var baseURL: URL? {
        let raw = ProcessInfo.processInfo.environment["SECONDHELLO_SERVER_URL"] ?? UserDefaults.standard.string(forKey: "serverURL") ?? ""
        guard !raw.isEmpty else { return nil }
        return URL(string: raw.hasSuffix("/") ? String(raw.dropLast()) : raw)
    }
    static func health() async throws -> ServerHealth {
        guard let baseURL else { throw WorkflowError.notConfigured }
        var request = URLRequest(url: baseURL.appendingPathComponent("health")); request.timeoutInterval = 2
        let (data, response) = try await URLSession.shared.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw WorkflowError.requestFailed }
        return try JSONDecoder.secondHello.decode(ServerHealth.self, from: data)
    }
    static func run(action: String, values: [String: Any] = [:]) async throws -> WorkflowResponse {
        guard let baseURL else { throw WorkflowError.notConfigured }
        var request = URLRequest(url: baseURL.appendingPathComponent("workflow"))
        // Extraction + embeddings can exceed 15 seconds on a cold provider call.
        request.timeoutInterval = 90
        request.httpMethod = "POST"; request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var payload: [String: Any] = ["action": action]
        values.forEach { payload[$0.key] = $0.value }
        request.httpBody = try JSONSerialization.data(withJSONObject: payload)
        let (data, response) = try await URLSession.shared.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw WorkflowError.requestFailed }
        return try JSONDecoder.secondHello.decode(WorkflowResponse.self, from: data)
    }
    static func elevenLabsSignedURL() async throws -> URL {
        guard let baseURL else { throw WorkflowError.notConfigured }
        var request = URLRequest(url: baseURL.appendingPathComponent("elevenlabs/signed-url")); request.timeoutInterval = 8
        let (data, response) = try await URLSession.shared.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200,
              let signed = try? JSONDecoder().decode(SignedConversation.self, from: data),
              signed.ok,
              let value = signed.signedUrl,
              let url = URL(string: value),
              url.scheme == "wss" else { throw WorkflowError.voiceAgentUnavailable }
        return url
    }
    static func jsonObject<T: Encodable>(_ value: T) throws -> Any {
        try JSONSerialization.jsonObject(with: JSONEncoder.secondHello.encode(value))
    }
    enum WorkflowError: LocalizedError {
        case notConfigured, requestFailed, voiceAgentUnavailable
        var errorDescription: String? {
            switch self {
            case .notConfigured: "Local workflow server not configured; using deterministic local mode."
            case .requestFailed: "Local workflow server unavailable; using deterministic local mode."
            case .voiceAgentUnavailable: "Authenticated ElevenLabs voice is not configured on the local server."
            }
        }
    }
}

enum MailDraftOpener {
    static func url(for draft: IntroductionDraft) -> URL? {
        var components = URLComponents(); components.scheme = "mailto"; components.path = draft.to
        var items = [URLQueryItem(name: "subject", value: draft.subject), URLQueryItem(name: "body", value: draft.body)]
        if !draft.cc.isEmpty { items.append(URLQueryItem(name: "cc", value: draft.cc)) }
        components.queryItems = items
        return components.url
    }
    @MainActor static func open(_ draft: IntroductionDraft) -> Bool {
        guard let url = url(for: draft) else { return false }
        return NSWorkspace.shared.open(url)
    }
}

@MainActor
final class LiveListeningService: ObservableObject {
    enum Phase: Equatable {
        case idle, requestingPermission, listening, reviewing, unavailable(String)
    }

    @Published private(set) var phase: Phase = .idle
    @Published private(set) var audioLevel: Double = 0
    @Published var transcript = ""
    @Published private(set) var engineLabel = "Apple Speech · on-device when available"

    private let audioEngine = AVAudioEngine()
    private var recognitionRequest: SFSpeechAudioBufferRecognitionRequest?
    private var recognitionTask: SFSpeechRecognitionTask?
    private var tapInstalled = false
    private var acceptingResults = false

    var isListening: Bool { phase == .listening }
    var statusText: String {
        switch phase {
        case .idle: "Ready when permission is granted"
        case .requestingPermission: "Requesting microphone access…"
        case .listening: "Listening live · transcript is not saved yet"
        case .reviewing: "Listening stopped · review before saving"
        case .unavailable(let message): message
        }
    }

    func start() async {
        guard !isListening else { return }
        phase = .requestingPermission
        guard await speechPermission() else { phase = .unavailable("Speech recognition permission was not granted."); return }
        guard await microphonePermission() else { phase = .unavailable("Microphone permission was not granted."); return }
        guard let recognizer = SFSpeechRecognizer(locale: .current), recognizer.isAvailable else { phase = .unavailable("Live speech recognition is currently unavailable."); return }

        recognitionTask?.cancel(); recognitionTask = nil
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        request.taskHint = .dictation
        if recognizer.supportsOnDeviceRecognition {
            request.requiresOnDeviceRecognition = true
            engineLabel = "Apple Speech · on-device"
        } else {
            engineLabel = "Apple Speech · system service"
        }
        recognitionRequest = request

        let input = audioEngine.inputNode
        if tapInstalled { input.removeTap(onBus: 0); tapInstalled = false }
        let format = input.outputFormat(forBus: 0)
        input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self, weak request] buffer, _ in
            request?.append(buffer)
            guard let samples = buffer.floatChannelData?[0] else { return }
            let count = Int(buffer.frameLength)
            guard count > 0 else { return }
            var power: Float = 0
            for index in 0..<count { power += samples[index] * samples[index] }
            let rms = sqrt(power / Float(count))
            let decibels = 20 * log10(max(rms, 0.000_01))
            let level = Double(min(max((decibels + 50) / 50, 0), 1))
            Task { @MainActor [weak self] in self?.audioLevel = level }
        }
        tapInstalled = true

        recognitionTask = recognizer.recognitionTask(with: request) { [weak self] result, error in
            let text = result?.bestTranscription.formattedString
            let final = result?.isFinal ?? false
            let failure = error?.localizedDescription
            Task { @MainActor [weak self] in
                guard let self, self.acceptingResults else { return }
                if let text { self.transcript = text }
                if let failure, !final { self.phase = .unavailable(failure); self.stopAudio() }
                else if final { self.stop() }
            }
        }

        do {
            audioEngine.prepare()
            try audioEngine.start()
            acceptingResults = true
            phase = .listening
        } catch {
            stopAudio(); phase = .unavailable("The microphone could not start: \(error.localizedDescription)")
        }
    }

    func stop() {
        stopAudio()
        if !transcript.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { phase = .reviewing }
        else { phase = .idle }
    }

    func reset() {
        stopAudio(); transcript = ""; audioLevel = 0; phase = .idle
    }

    private func stopAudio() {
        acceptingResults = false
        if audioEngine.isRunning { audioEngine.stop() }
        if tapInstalled { audioEngine.inputNode.removeTap(onBus: 0); tapInstalled = false }
        recognitionRequest?.endAudio()
        recognitionTask?.cancel()
        recognitionTask = nil; recognitionRequest = nil; audioLevel = 0
    }

    private func speechPermission() async -> Bool {
        if SFSpeechRecognizer.authorizationStatus() == .authorized { return true }
        return await withCheckedContinuation { continuation in
            SFSpeechRecognizer.requestAuthorization { continuation.resume(returning: $0 == .authorized) }
        }
    }

    private func microphonePermission() async -> Bool {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: return true
        case .notDetermined: return await AVCaptureDevice.requestAccess(for: .audio)
        default: return false
        }
    }
}

enum ElevenLabsAudioFormat {
    static func sampleRate(from value: String) -> Double? {
        guard value.hasPrefix("pcm_"), let rate = Double(value.dropFirst(4)), rate > 0 else { return nil }
        return rate
    }
}

private final class AudioBufferSupply: @unchecked Sendable {
    let buffer: AVAudioPCMBuffer
    var supplied = false
    init(_ buffer: AVAudioPCMBuffer) { self.buffer = buffer }
}

/// Core Audio invokes taps on a realtime queue. Keep every operation on that
/// queue outside MainActor, then deliver the converted bytes to UI state.
private final class ElevenLabsInputAudioPipeline: @unchecked Sendable {
    let converter: AVAudioConverter
    let targetFormat: AVAudioFormat
    let onAudio: @MainActor @Sendable (Double, Data) -> Void

    init(converter: AVAudioConverter, targetFormat: AVAudioFormat, onAudio: @escaping @MainActor @Sendable (Double, Data) -> Void) {
        self.converter = converter
        self.targetFormat = targetFormat
        self.onAudio = onAudio
    }

    func process(_ input: AVAudioPCMBuffer) {
        let level = Self.level(for: input)
        guard let pcm = Self.convert(input, using: converter, targetFormat: targetFormat) else { return }
        Task { @MainActor [onAudio] in onAudio(level, pcm) }
    }

    private static func level(for buffer: AVAudioPCMBuffer) -> Double {
        guard let samples = buffer.floatChannelData?[0] else { return 0 }
        let count = Int(buffer.frameLength)
        guard count > 0 else { return 0 }
        var power: Float = 0
        for index in 0..<count { power += samples[index] * samples[index] }
        let rms = sqrt(power / Float(count))
        let decibels = 20 * log10(max(rms, 0.000_01))
        return Double(min(max((decibels + 50) / 50, 0), 1))
    }

    private static func convert(_ input: AVAudioPCMBuffer, using converter: AVAudioConverter, targetFormat: AVAudioFormat) -> Data? {
        let ratio = targetFormat.sampleRate / input.format.sampleRate
        let capacity = AVAudioFrameCount(ceil(Double(input.frameLength) * ratio)) + 1
        guard let output = AVAudioPCMBuffer(pcmFormat: targetFormat, frameCapacity: capacity) else { return nil }
        let supply = AudioBufferSupply(input)
        var conversionError: NSError?
        let status = converter.convert(to: output, error: &conversionError) { _, status in
            if supply.supplied { status.pointee = .noDataNow; return nil }
            supply.supplied = true; status.pointee = .haveData; return supply.buffer
        }
        guard conversionError == nil, status != .error, output.frameLength > 0 else { return nil }
        guard let samples = output.int16ChannelData?[0] else { return nil }
        return Data(bytes: samples, count: Int(output.frameLength) * MemoryLayout<Int16>.size)
    }
}

private func installElevenLabsInputTap(on input: AVAudioInputNode, format: AVAudioFormat, pipeline: ElevenLabsInputAudioPipeline) {
    input.installTap(onBus: 0, bufferSize: 1_024, format: format) { buffer, _ in
        pipeline.process(buffer)
    }
}

/// Authenticated, two-way ElevenLabs Agents session. The API key never enters
/// this process: the app receives only a short-lived signed WebSocket URL from
/// the localhost LangGraph server after the in-app consent gate is unlocked.
@MainActor
final class ElevenLabsConversationService: ObservableObject {
    enum Phase: Equatable {
        case idle, requestingPermission, connecting, listening, reviewing, unavailable(String)
    }

    @Published private(set) var phase: Phase = .idle
    @Published private(set) var audioLevel: Double = 0
    @Published private(set) var agentResponse = ""
    @Published private(set) var conversationID: String?
    @Published private(set) var isAgentSpeaking = false
    @Published var transcript = ""

    private let inputEngine = AVAudioEngine()
    private let outputEngine = AVAudioEngine()
    private let playerNode = AVAudioPlayerNode()
    private var inputConverter: AVAudioConverter?
    private var inputPipeline: ElevenLabsInputAudioPipeline?
    private var outputFormat: AVAudioFormat?
    private var socket: URLSessionWebSocketTask?
    private var receiveTask: Task<Void, Never>?
    private var sendTail: Task<Void, Never>?
    private var tapInstalled = false
    private var queuedAudioChunks = 0
    private var intentionalStop = false

    var isListening: Bool { phase == .listening }
    var isActive: Bool { phase == .connecting || phase == .listening }
    var engineLabel: String { "ElevenLabs · private background session" }
    var statusText: String {
        switch phase {
        case .idle: "Ready for a private voice conversation"
        case .requestingPermission: "Requesting microphone access…"
        case .connecting: "Opening a short-lived authenticated voice session…"
        case .listening: isAgentSpeaking ? "Second Hello is responding · interrupt anytime" : "Listening quietly in the background · say “Second Hello” for help"
        case .reviewing: "Voice session ended · review before saving"
        case .unavailable(let message): message
        }
    }

    init() {
        outputEngine.attach(playerNode)
    }

    func start() async throws {
        guard !isActive else { return }
        phase = .requestingPermission
        guard await microphonePermission() else {
            phase = .unavailable("Microphone permission was not granted.")
            throw VoiceAgentError.microphonePermission
        }

        phase = .connecting
        let signedURL: URL
        do { signedURL = try await WorkflowClient.elevenLabsSignedURL() }
        catch {
            phase = .unavailable(error.localizedDescription)
            throw error
        }

        do {
            try configureOutput(sampleRate: 16_000)
            try configureInput()
        } catch {
            stopAudio()
            phase = .unavailable("Realtime audio could not start: \(error.localizedDescription)")
            throw error
        }

        intentionalStop = false
        let task = URLSession.shared.webSocketTask(with: signedURL)
        socket = task
        task.resume()
        receiveEvents()
        sendJSON(["type": "conversation_initiation_client_data"])
        phase = .listening
    }

    func stop() {
        intentionalStop = true
        receiveTask?.cancel(); receiveTask = nil
        sendTail?.cancel(); sendTail = nil
        socket?.cancel(with: .normalClosure, reason: nil); socket = nil
        stopAudio()
        phase = transcript.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? .idle : .reviewing
    }

    func reset() {
        stop(); transcript = ""; agentResponse = ""; conversationID = nil; phase = .idle
    }

    private func configureInput() throws {
        let input = inputEngine.inputNode
        if tapInstalled { input.removeTap(onBus: 0); tapInstalled = false }
        // Raw input is the reliable default across built-in, USB, and virtual
        // microphones. Voice processing remains opt-in for known-good devices.
        if ProcessInfo.processInfo.environment["SECONDHELLO_VOICE_PROCESSING_ENABLED"] == "1" {
            try? input.setVoiceProcessingEnabled(true)
        }
        let sourceFormat = input.outputFormat(forBus: 0)
        guard let targetFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16_000, channels: 1, interleaved: false),
              let converter = AVAudioConverter(from: sourceFormat, to: targetFormat) else {
            throw VoiceAgentError.unsupportedAudioFormat
        }
        inputConverter = converter
        let pipeline = ElevenLabsInputAudioPipeline(converter: converter, targetFormat: targetFormat) { [weak self] level, pcm in
            guard let self, self.isListening else { return }
            self.audioLevel = level
            self.sendJSON(["user_audio_chunk": pcm.base64EncodedString()])
        }
        inputPipeline = pipeline
        installElevenLabsInputTap(on: input, format: sourceFormat, pipeline: pipeline)
        tapInstalled = true
        inputEngine.prepare()
        try inputEngine.start()
    }

    private func configureOutput(sampleRate: Double) throws {
        if outputEngine.isRunning { outputEngine.stop() }
        playerNode.stop()
        outputEngine.disconnectNodeOutput(playerNode)
        guard let format = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: sampleRate, channels: 1, interleaved: true) else {
            throw VoiceAgentError.unsupportedAudioFormat
        }
        outputFormat = format
        outputEngine.connect(playerNode, to: outputEngine.mainMixerNode, format: format)
        outputEngine.prepare()
        try outputEngine.start()
        playerNode.play()
    }

    private func receiveEvents() {
        receiveTask?.cancel()
        receiveTask = Task { @MainActor [weak self] in
            guard let self else { return }
            while !Task.isCancelled, let socket = self.socket {
                do {
                    let message = try await socket.receive()
                    let data: Data
                    switch message {
                    case .string(let value): data = Data(value.utf8)
                    case .data(let value): data = value
                    @unknown default: continue
                    }
                    self.handleEvent(data)
                } catch {
                    if !self.intentionalStop {
                        self.stopAudio()
                        self.socket = nil
                        self.phase = .unavailable("Voice session disconnected; the transcript remains reviewable.")
                    }
                    return
                }
            }
        }
    }

    private func handleEvent(_ data: Data) {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any], let type = object["type"] as? String else { return }
        switch type {
        case "conversation_initiation_metadata":
            guard let metadata = object["conversation_initiation_metadata_event"] as? [String: Any] else { return }
            conversationID = metadata["conversation_id"] as? String
            if let audioName = metadata["agent_output_audio_format"] as? String,
               let rate = ElevenLabsAudioFormat.sampleRate(from: audioName),
               outputFormat?.sampleRate != rate {
                try? configureOutput(sampleRate: rate)
            }
        case "user_transcript":
            guard let event = object["user_transcription_event"] as? [String: Any],
                  let value = event["user_transcript"] as? String else { return }
            appendTranscript(value)
        case "agent_response":
            guard let event = object["agent_response_event"] as? [String: Any],
                  let value = event["agent_response"] as? String else { return }
            agentResponse = value
        case "agent_response_correction":
            guard let event = object["agent_response_correction_event"] as? [String: Any],
                  let value = event["corrected_agent_response"] as? String else { return }
            agentResponse = value
        case "audio":
            guard let event = object["audio_event"] as? [String: Any],
                  let encoded = event["audio_base_64"] as? String,
                  let pcm = Data(base64Encoded: encoded) else { return }
            play(pcm)
        case "interruption":
            interruptPlayback()
        case "ping":
            guard let event = object["ping_event"] as? [String: Any], let eventID = event["event_id"] else { return }
            let delay = (event["ping_ms"] as? NSNumber)?.doubleValue ?? 0
            Task { @MainActor [weak self] in
                if delay > 0 { try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000)) }
                self?.sendJSON(["type": "pong", "event_id": eventID])
            }
        case "client_error", "guardrail_triggered":
            stopAudio(); socket?.cancel(with: .goingAway, reason: nil); socket = nil
            phase = .unavailable(type == "guardrail_triggered" ? "The voice safety guardrail ended this session." : "ElevenLabs reported a voice-session error.")
        default: break
        }
    }

    private func appendTranscript(_ value: String) {
        let cleaned = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard cleaned.unicodeScalars.contains(where: CharacterSet.alphanumerics.contains) else { return }
        let lines = transcript.split(separator: "\n").map(String.init)
        guard lines.last != cleaned else { return }
        transcript += (transcript.isEmpty ? "" : "\n") + cleaned
    }

    private func play(_ data: Data) {
        guard let format = outputFormat, data.count >= MemoryLayout<Int16>.size else { return }
        let frames = AVAudioFrameCount(data.count / MemoryLayout<Int16>.size)
        guard let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames) else { return }
        buffer.frameLength = frames
        let audioBuffer = buffer.mutableAudioBufferList.pointee.mBuffers
        guard let destination = audioBuffer.mData else { return }
        data.copyBytes(to: destination.assumingMemoryBound(to: UInt8.self), count: min(data.count, Int(audioBuffer.mDataByteSize)))
        queuedAudioChunks += 1; isAgentSpeaking = true
        if !playerNode.isPlaying { playerNode.play() }
        playerNode.scheduleBuffer(buffer) { [weak self] in
            Task { @MainActor [weak self] in
                guard let self else { return }
                self.queuedAudioChunks = max(0, self.queuedAudioChunks - 1)
                if self.queuedAudioChunks == 0 { self.isAgentSpeaking = false }
            }
        }
    }

    private func interruptPlayback() {
        playerNode.stop(); playerNode.reset(); queuedAudioChunks = 0; isAgentSpeaking = false
        playerNode.play()
    }

    private func sendJSON(_ value: [String: Any]) {
        guard let socket, let data = try? JSONSerialization.data(withJSONObject: value), let text = String(data: data, encoding: .utf8) else { return }
        let previous = sendTail
        sendTail = Task {
            if let previous { await previous.value }
            guard !Task.isCancelled else { return }
            try? await socket.send(.string(text))
        }
    }

    private func stopAudio() {
        if inputEngine.isRunning { inputEngine.stop() }
        if tapInstalled { inputEngine.inputNode.removeTap(onBus: 0); tapInstalled = false }
        inputConverter = nil; inputPipeline = nil
        playerNode.stop(); playerNode.reset()
        if outputEngine.isRunning { outputEngine.stop() }
        queuedAudioChunks = 0; isAgentSpeaking = false; audioLevel = 0
    }

    private func microphonePermission() async -> Bool {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: return true
        case .notDetermined: return await AVCaptureDevice.requestAccess(for: .audio)
        default: return false
        }
    }

    enum VoiceAgentError: LocalizedError {
        case microphonePermission, unsupportedAudioFormat
        var errorDescription: String? {
            switch self {
            case .microphonePermission: "Microphone permission is required for live voice."
            case .unsupportedAudioFormat: "This microphone audio format cannot be converted to ElevenLabs PCM."
            }
        }
    }
}

enum LocalExtractor {
    static func extract(_ transcript: String, conversationID: UUID) -> Profile {
        let sentences = transcript.split(whereSeparator: { ".!?".contains($0) }).map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
        func snippets(_ markers: [String]) -> [String] { sentences.filter { s in markers.contains { s.localizedCaseInsensitiveContains($0) } } }
        func clean(_ lines: [String], _ marker: String) -> [String] {
            lines.map { $0.replacingOccurrences(of: "(?i).*?\\b" + marker + "\\b", with: "", options: .regularExpression).trimmingCharacters(in: CharacterSet(charactersIn: " :,-")) }.filter { !$0.isEmpty }
        }
        let needs = clean(snippets(["need", "looking for"]), "(?:need|looking for)")
        let offers = clean(snippets(["can offer", "i offer", "i build"]), "(?:can offer|i offer|i build)")
        let topics = clean(snippets(["interested in", "care about"]), "(?:interested in|care about)")
        let commitments = snippets(["i will", "i'll", "will send", "will share"])
        let evidence = sentences.map { Evidence(quote: $0, conversationID: conversationID, capturedAt: .now) }
        return Profile(needs: needs, offers: offers, topics: topics, commitments: commitments, evidence: evidence)
    }
}

enum Keychain {
    static func save(_ value: String, account: String) {
        let data = Data(value.utf8)
        SecItemDelete([kSecClass: kSecClassGenericPassword, kSecAttrService: "SecondHello", kSecAttrAccount: account] as CFDictionary)
        SecItemAdd([kSecClass: kSecClassGenericPassword, kSecAttrService: "SecondHello", kSecAttrAccount: account, kSecValueData: data] as CFDictionary, nil)
    }
    static func read(account: String) -> String? {
        var result: CFTypeRef?
        let status = SecItemCopyMatching([kSecClass: kSecClassGenericPassword, kSecAttrService: "SecondHello", kSecAttrAccount: account, kSecReturnData: true] as CFDictionary, &result)
        guard status == errSecSuccess, let data = result as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }
}

enum ElevenLabsTranscriber {
    /// Uploads a user-selected audio file only after the consent toggle is enabled in the UI.
    static func transcribe(fileURL: URL) async throws -> String {
        guard let key = Keychain.read(account: "elevenlabs-api-key"), !key.isEmpty else { throw TranscriptionError.missingKey }
        let audio = try Data(contentsOf: fileURL)
        let boundary = "SecondHello-\(UUID().uuidString)"
        var body = Data()
        func append(_ string: String) { body.append(Data(string.utf8)) }
        append("--\(boundary)\r\nContent-Disposition: form-data; name=\"model_id\"\r\n\r\nscribe_v2\r\n")
        append("--\(boundary)\r\nContent-Disposition: form-data; name=\"file\"; filename=\"\(fileURL.lastPathComponent)\"\r\nContent-Type: audio/mpeg\r\n\r\n")
        body.append(audio); append("\r\n--\(boundary)--\r\n")
        var request = URLRequest(url: URL(string: "https://api.elevenlabs.io/v1/speech-to-text")!)
        request.httpMethod = "POST"; request.setValue(key, forHTTPHeaderField: "xi-api-key")
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type"); request.httpBody = body
        let (data, response) = try await URLSession.shared.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200,
              let object = try JSONSerialization.jsonObject(with: data) as? [String: Any], let text = object["text"] as? String else { throw TranscriptionError.requestFailed }
        return text
    }
    enum TranscriptionError: LocalizedError { case missingKey, requestFailed; var errorDescription: String? { self == .missingKey ? "Add an ElevenLabs key in Settings first." : "ElevenLabs transcription did not return text." } }
}

@MainActor
final class BriefingSpeaker: NSObject, ObservableObject, AVAudioPlayerDelegate {
    @Published var status = "Private: never sent externally"
    private var player: AVAudioPlayer?
    private let localSpeaker = AVSpeechSynthesizer()
    func speak(_ text: String) {
        guard let key = Keychain.read(account: "elevenlabs-api-key"), !key.isEmpty else {
            localSpeaker.speak(AVSpeechUtterance(string: text)); status = "Spoken locally (ElevenLabs key not configured)"; return
        }
        status = "Generating private ElevenLabs briefing…"
        Task {
            do {
                var request = URLRequest(url: URL(string: "https://api.elevenlabs.io/v1/text-to-speech/21m00Tcm4TlvDq8ikWAM")!)
                request.httpMethod = "POST"; request.setValue(key, forHTTPHeaderField: "xi-api-key"); request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                request.httpBody = try JSONSerialization.data(withJSONObject: ["text": text, "model_id": "eleven_multilingual_v2"])
                let (data, response) = try await URLSession.shared.data(for: request)
                guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw URLError(.badServerResponse) }
                player = try AVAudioPlayer(data: data); player?.play(); status = "Spoken privately through ElevenLabs"
            } catch { status = "ElevenLabs unavailable; using local speech"; localSpeaker.speak(AVSpeechUtterance(string: text)) }
        }
    }
}
