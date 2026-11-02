import AppKit
import Combine
import Foundation
import ServiceManagement

@MainActor
final class MenuStore: ObservableObject {
    @Published var snapshot: MenuSnapshot?
    @Published var peers: [Peer] = []
    @Published var selectedHost = "local"
    @Published var tab = "Overview"
    @Published var busy = false
    @Published var error: String?
    @Published var notice: String?
    @Published var confirmationPresented = false
    @Published var review: Review?
    @Published var reviewCreated: Date?
    @Published var loginEnabled = SMAppService.mainApp.status == .enabled
    @Published var loginNote: String?
    private var generation = UUID()
    private var started = false
    var local: Bool { selectedHost == "local" }
    var peer: Peer? { peers.first { $0.id == selectedHost } }
    var canConfirm: Bool {
        ActionPolicy.canCollect(local: local,
          fresh: reviewCreated.map { Date().timeIntervalSince($0) >= 0 && Date().timeIntervalSince($0) < Double(review?.expiresInSeconds ?? 0) } ?? false,
          candidateCount: review?.candidateCount ?? 0, busy: busy)
    }

    func start() {
        guard !started else { return }
        started = true
        refresh()
        updateLoginStatus()
        Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 60_000_000_000)
                guard let self else { return }
                if self.review == nil && !self.busy { self.refresh() }
            }
        }
    }

    func chooseHost(_ id: String) {
        guard !busy, id == "local" || peers.contains(where: { $0.id == id }) else { return }
        generation = UUID()
        selectedHost = id
        snapshot = nil
        discardReview()
        notice = nil
        error = nil
        refresh()
    }

    func discardReview() {
        confirmationPresented = false
        review = nil
        reviewCreated = nil
    }

    func refresh() {
        guard !busy else { return }
        let token = generation
        let selectedPeer = peer
        guard local || selectedPeer != nil else { error = "This host is not configured."; return }
        busy = true
        error = nil
        Task {
            defer { busy = false }
            do {
                let value = try MenuSnapshot.decode(await Gateway.request(["snapshot"], peer: selectedPeer))
                guard token == generation else { return }
                guard Freshness.isFresh(value.date) else { throw GatewayError.invalidResponse }
                snapshot = value
                if selectedPeer == nil { peers = value.peers }
            } catch {
                guard token == generation else { return }
                self.error = error.localizedDescription
                // Never leave old numbers looking like a successful refresh.
                snapshot = nil
            }
        }
    }

    func prepare(_ task: ManagedTask) {
        guard local, !busy, Freshness.isFresh(snapshot?.date) else { return }
        busy = true
        error = nil
        discardReview()
        notice = nil
        let token = generation
        Task {
            defer { busy = false }
            do {
                let data = try await Gateway.request(["preview", "--task-id", task.id])
                let value = try Display.decoder.decode(Review.self, from: data)
                guard token == generation, local, value.taskId == task.id,
                      value.candidateCount >= 0, value.candidateBytes >= 0,
                      value.expiresInSeconds > 0, value.expiresInSeconds <= 60,
                      value.reviewId.range(of: "^[0-9a-f]{32}$", options: .regularExpression) != nil else { throw GatewayError.invalidResponse }
                review = value
                reviewCreated = Date()
                tab = "Review"
            } catch { self.error = error.localizedDescription }
        }
    }

    func confirmCollection() {
        confirmationPresented = false
        guard canConfirm, let selectedReview = review else { error = "This review expired. Request a fresh preview."; return }
        busy = true
        review = nil
        reviewCreated = nil
        error = nil
        Task {
            do {
                let data = try await Gateway.request(["collect", "--review-id", selectedReview.reviewId, "--confirm"])
                let value = try Display.decoder.decode(CollectionResult.self, from: data)
                guard ActionPolicy.collectionSucceeded(value.status, reconciliationRequired: value.reconciliationRequired) else { throw GatewayError.refused }
                notice = "\(value.status) · \(Display.bytes(value.newRemovedBytes)) newly removed. Physical space reclaimed: unknown."
                tab = "History"
            } catch { self.error = error.localizedDescription }
            busy = false
            if error == nil { refresh() }
        }
    }

    func setLogin(_ enabled: Bool) {
        do {
            if enabled { try SMAppService.mainApp.register() }
            else { try SMAppService.mainApp.unregister() }
        } catch { loginNote = "Login registration could not be changed: \(error.localizedDescription)" }
        updateLoginStatus()
    }

    func updateLoginStatus() {
        let status = SMAppService.mainApp.status
        loginEnabled = status == .enabled
        if status == .requiresApproval { loginNote = "Approval is required in System Settings → Login Items." }
        else if status == .enabled { loginNote = "Registered with macOS. Starts at your next login." }
        else if status == .notRegistered { loginNote = "Launch at login is off." }
    }

    func openConfiguration() {
        let path = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".local/share/agent-pooper-scooper/menubar/config.json")
        if FileManager.default.fileExists(atPath: path.path) { NSWorkspace.shared.activateFileViewerSelecting([path]) }
        else { error = "Configuration is not initialized. Run the bundled installer first." }
    }
}
