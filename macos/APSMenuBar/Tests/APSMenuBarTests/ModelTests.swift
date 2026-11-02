import XCTest
import AppKit
@testable import APSMenuBar
final class ModelTests: XCTestCase {
    @MainActor func testStatusItemHasConcreteAccessibilityPressAction() {
        _ = NSApplication.shared
        let controller = ScoopStatusController()
        defer { controller.remove() }
        XCTAssertNotNil(controller.item.button?.target)
        XCTAssertNotNil(controller.item.button?.action)
        XCTAssertEqual(controller.item.button?.accessibilityLabel(), "Agent Pooper Scooper")
        XCTAssertEqual(controller.popover.behavior, .transient)
    }
    @MainActor func testDiscardedReviewClosesItsConfirmation() {
        let store = MenuStore()
        store.confirmationPresented = true
        store.discardReview()
        XCTAssertFalse(store.confirmationPresented)
        XCTAssertNil(store.review)
        XCTAssertNil(store.reviewCreated)
        XCTAssertFalse(store.canConfirm)
    }
    func testAutomaticStatusDecodesWithoutEnrollingDiscoveredAgents() throws {
        let raw = Data(#"{"status":"available","enabled":false,"enrolled_runs":0,"retention_seconds":86400,"removed_bytes":0,"coverage":"new APS capture only"}"#.utf8)
        let value = try Display.decoder.decode(AutomaticStatus.self, from: raw)
        XCTAssertEqual(value.enrolledRuns, 0)
        XCTAssertEqual(value.enabled, false)
        XCTAssertEqual(value.retentionSeconds, 86400)
    }
    func testUnknownDiscoveryNeverDisplaysZero() throws {
        let raw = Data(#"{"status":"unknown","providers":[],"cleanup_authority":"none"}"#.utf8)
        let value = try Display.decoder.decode(AgentPresence.self, from: raw)
        XCTAssertNil(value.recognizableCount)
    }
    func testPeerAliasesRejectShellAndOptions() {
        for value in ["-oBad", "host;command", "user@host", "a\nb", "$(id)", ""] { XCTAssertFalse(Peer.validAlias(value)) }
        for value in ["kingston", "mac-2.example", "100.64.0.2"] { XCTAssertTrue(Peer.validAlias(value)) }
    }
    func testTimestampIsNeitherStaleNorFuture() {
        let now = Date(timeIntervalSince1970: 1000)
        XCTAssertTrue(Freshness.isFresh(Date(timeIntervalSince1970: 995), now: now))
        XCTAssertFalse(Freshness.isFresh(Date(timeIntervalSince1970: 800), now: now))
        XCTAssertFalse(Freshness.isFresh(Date(timeIntervalSince1970: 1030), now: now))
        XCTAssertFalse(Freshness.isFresh(nil, now: now))
    }
    func testUnknownBytesAreNotDisplayedAsZero() {
        XCTAssertEqual(Display.bytes(nil), "—")
        XCTAssertNotEqual(Display.bytes(0), "—")
    }
    func testOnlyFreshLocalEligibleReviewCanExecute() {
        XCTAssertTrue(ActionPolicy.canCollect(local: true, fresh: true, candidateCount: 1, busy: false))
        XCTAssertFalse(ActionPolicy.canCollect(local: false, fresh: true, candidateCount: 1, busy: false))
        XCTAssertFalse(ActionPolicy.canCollect(local: true, fresh: false, candidateCount: 1, busy: false))
        XCTAssertFalse(ActionPolicy.canCollect(local: true, fresh: true, candidateCount: 0, busy: false))
        XCTAssertFalse(ActionPolicy.canCollect(local: true, fresh: true, candidateCount: 1, busy: true))
    }
    func testRecoveryOutcomesAreNeverSuccessNotices() {
        XCTAssertTrue(ActionPolicy.collectionSucceeded("COMPLETED", reconciliationRequired: false))
        for status in ["RECOVERY_REQUIRED", "PARTIAL", "unknown"] {
            XCTAssertFalse(ActionPolicy.collectionSucceeded(status, reconciliationRequired: false))
        }
        XCTAssertFalse(ActionPolicy.collectionSucceeded("COMPLETED", reconciliationRequired: true))
    }
    func testMalformedSchemaAndCapacityRefuse() throws {
        let bad = Data(#"{"schema":"wrong","schema_version":1,"version":"a","observed_at":"2026-01-01T00:00:00Z","host":"fixture","volumes":[],"tasks":[],"peers":[],"history":[],"inventory_status":"unknown","inventory_complete":false,"limitations":[]}"#.utf8)
        XCTAssertThrowsError(try MenuSnapshot.decode(bad))
    }
}
