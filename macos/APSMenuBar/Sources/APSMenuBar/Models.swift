import Foundation

struct Volume: Decodable {
    let totalBytes: Int64
    let availableBytes: Int64
    let availablePercent: Double?
    let pressure: String
    var usedFraction: Double { totalBytes > 0 ? min(1, max(0, 1 - Double(availableBytes) / Double(totalBytes))) : 0 }
}

struct ManagedTask: Decodable, Identifiable {
    let id: String
    let label: String
    let status: String
    let retainedBytes: Int64?
    let eligibleBytes: Int64?
    let removedBytes: Int64?
    let complete: Bool
}

struct Peer: Decodable, Identifiable, Equatable {
    let id: String
    let label: String
    let sshAlias: String
    static func validAlias(_ value: String) -> Bool {
        value.range(of: "^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$", options: .regularExpression) != nil && !value.contains("\n")
    }
}

struct HistoryItem: Decodable, Identifiable {
    let reviewId: String
    let label: String
    let observedAt: String
    let status: String
    let removedBytes: Int64?
    var id: String { reviewId }
}

struct AutomaticStatus: Decodable {
    let status: String
    let enabled: Bool?
    let enrolledRuns: Int?
    let retentionSeconds: Int?
    let removedBytes: Int64?
    let coverage: String
}

struct AgentPresence: Decodable {
    struct Provider: Decodable {
        let provider: String
        let count: Int
        let confidence: String
    }
    let status: String
    let providers: [Provider]
    let cleanupAuthority: String
    var recognizableCount: Int? {
        guard status == "observed", providers.allSatisfy({ $0.count > 0 && $0.count <= 16384 }) else { return nil }
        return providers.reduce(0) { $0 + $1.count }
    }
}

struct MenuSnapshot: Decodable {
    let autopilot: AutomaticStatus?
    let discovery: AgentPresence?
    let schema: String
    let schemaVersion: Int
    let version: String
    let observedAt: String
    let host: String
    let volumes: [Volume]
    let tasks: [ManagedTask]
    let peers: [Peer]
    let history: [HistoryItem]
    let eligibleBytes: Int64?
    let unresolvedReceipts: Int?
    let inventoryStatus: String
    let inventoryObservedAt: String?
    let inventoryBytes: Int64?
    let inventoryComplete: Bool
    let limitations: [String]
    var date: Date? { Display.date(observedAt) }
    static func decode(_ data: Data) throws -> MenuSnapshot {
        let value = try Display.decoder.decode(Self.self, from: data)
        guard value.schema == "agent-pooper-scooper/menu", value.schemaVersion == 1,
              value.tasks.count <= 12, value.peers.count <= 4, value.history.count <= 20,
              value.volumes.count <= 12, value.date != nil,
              value.volumes.allSatisfy({ $0.totalBytes > 0 && $0.availableBytes >= 0 && $0.availableBytes <= $0.totalBytes }),
              value.peers.allSatisfy({ Peer.validAlias($0.sshAlias) && $0.id != "local" }),
              Set(value.tasks.map(\.id)).count == value.tasks.count,
              Set(value.peers.map(\.id)).count == value.peers.count else { throw GatewayError.invalidResponse }
        return value
    }
}

struct Review: Decodable {
    let reviewId: String
    let label: String
    let taskId: String
    let candidateBytes: Int64
    let candidateCount: Int
    let retainedCount: Int
    let reasons: [String]
    let expiresInSeconds: Int
}

struct CollectionResult: Decodable {
    let status: String
    let removedBytes: Int64?
    let newRemovedBytes: Int64?
    let historicalReplay: Bool
    let reconciliationRequired: Bool
}

enum Freshness {
    static func isFresh(_ date: Date?, now: Date = Date()) -> Bool {
        guard let date else { return false }
        let age = now.timeIntervalSince(date)
        return age >= -5 && age <= 120
    }
}

enum ActionPolicy {
    static func collectionSucceeded(_ status: String, reconciliationRequired: Bool) -> Bool {
        status == "COMPLETED" && !reconciliationRequired
    }
    static func canCollect(local: Bool, fresh: Bool, candidateCount: Int, busy: Bool) -> Bool {
        local && fresh && candidateCount > 0 && !busy
    }
}

enum Display {
    static var decoder: JSONDecoder { let d = JSONDecoder(); d.keyDecodingStrategy = .convertFromSnakeCase; return d }
    static func bytes(_ value: Int64?) -> String {
        guard let value else { return "—" }
        let formatter = ByteCountFormatter()
        formatter.countStyle = .file
        return formatter.string(fromByteCount: value)
    }
    static func date(_ value: String) -> Date? {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f.date(from: value) ?? ISO8601DateFormatter().date(from: value)
    }
    static func time(_ value: String?) -> String {
        guard let value, let date = date(value) else { return "Not measured" }
        return date.formatted(date: .abbreviated, time: .shortened)
    }
}
