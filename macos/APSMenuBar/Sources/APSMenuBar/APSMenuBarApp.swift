import AppKit
import SwiftUI

@main
enum APSMenuBarApp {
    @MainActor static func main() {
        let application = NSApplication.shared
        let delegate = ScoopAppDelegate()
        application.delegate = delegate
        // A pure status utility has no empty Settings scene to open on launch.
        withExtendedLifetime(delegate) { application.run() }
    }
}

@MainActor
final class ScoopAppDelegate: NSObject, NSApplicationDelegate {
    private var controller: ScoopStatusController?
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApplication.shared.setActivationPolicy(.accessory)
        controller = ScoopStatusController()
        controller?.store.start()
    }
}

/// Concrete AppKit target/action keeps AXPress and VoiceOver usable even on
/// system versions where SwiftUI's window-style status item drops its action.
@MainActor
final class ScoopStatusController: NSObject {
    let store = MenuStore()
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    let popover = NSPopover()

    override init() {
        super.init()
        popover.behavior = .transient
        popover.animates = false
        popover.contentViewController = NSHostingController(rootView: ScoopPanel(store: store))
        popover.contentSize = NSSize(width: 360, height: 574)
        if let button = item.button {
            button.image = NSImage(systemSymbolName: "sparkles", accessibilityDescription: "Agent Pooper Scooper")
            button.image?.isTemplate = true
            button.setAccessibilityLabel("Agent Pooper Scooper")
            button.toolTip = "Agent Pooper Scooper"
            button.target = self
            button.action = #selector(togglePopover(_:))
        }
    }

    @objc func togglePopover(_ sender: Any?) {
        if popover.isShown { popover.performClose(sender); return }
        guard let button = item.button else { return }
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        NSApplication.shared.activate(ignoringOtherApps: true)
        popover.contentViewController?.view.window?.makeKey()
    }

    func remove() {
        popover.close()
        NSStatusBar.system.removeStatusItem(item)
    }
}

struct ScoopPanel: View {
    @ObservedObject var store: MenuStore
    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            VStack(spacing: 14) {
                HStack {
                    Picker("Computer", selection: Binding(get: { store.selectedHost }, set: { store.chooseHost($0) })) {
                        Text("This Mac").tag("local")
                        ForEach(store.peers) { Text($0.label).tag($0.id) }
                    }
                    .labelsHidden().disabled(store.busy)
                    Spacer()
                    if store.busy { ProgressView().controlSize(.small).accessibilityLabel("Refreshing") }
                    else {
                        Button { store.refresh() } label: { Image(systemName: "arrow.clockwise") }
                            .buttonStyle(.plain).help("Refresh data").accessibilityLabel("Refresh data")
                            .keyboardShortcut("r")
                    }
                }
                Picker("Section", selection: $store.tab) {
                    ForEach(["Overview", "Review", "History", "Settings"], id: \.self) { Text($0).tag($0) }
                }.pickerStyle(.segmented).labelsHidden()
                ScrollView {
                    VStack(alignment: .leading, spacing: 14) {
                        if let error = store.error { message(error, symbol: "exclamationmark.triangle", warning: true) }
                        if let notice = store.notice { message(notice, symbol: "checkmark.circle", warning: false) }
                        switch store.tab {
                        case "Review": review
                        case "History": history
                        case "Settings": settings
                        default: overview
                        }
                    }.frame(maxWidth: .infinity, alignment: .leading).padding(.bottom, 8)
                }.frame(height: 350)
            }.padding(18)
            Divider()
            HStack {
                Label("Safety first", systemImage: "lock.shield").foregroundStyle(.secondary)
                Spacer()
                Text("Preview").foregroundStyle(.secondary)
                Button("Quit") { NSApplication.shared.terminate(nil) }.buttonStyle(.plain).keyboardShortcut("q")
            }.font(.caption).padding(.horizontal, 18).padding(.vertical, 12)
        }
        .frame(width: 360)
        .background(.regularMaterial)
        .onAppear { store.start() }
        .confirmationDialog("Remove these managed scratch files?", isPresented: $store.confirmationPresented, titleVisibility: .visible) {
            Button("Remove reviewed files", role: .destructive) { store.confirmCollection() }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Only the exact reviewed APS objects can be removed. This is irreversible. Source files, sessions and native caches are not included. Physical SSD space reclaimed remains unknown.")
        }
    }

    private var header: some View {
        HStack(spacing: 10) {
            Image(systemName: "sparkles").font(.system(size: 22, weight: .medium)).foregroundStyle(.green)
                .frame(width: 38, height: 38).background(.green.opacity(0.1), in: RoundedRectangle(cornerRadius: 11))
            VStack(alignment: .leading, spacing: 3) {
                Text("Agent Pooper Scooper").font(.system(size: 15, weight: .semibold))
                Text("A little less mess. A little more space.").font(.system(size: 10)).foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
        }.padding(18)
    }

    @ViewBuilder private var overview: some View {
        if let value = store.snapshot {
            TimelineView(.periodic(from: .now, by: 5)) { context in
                let fresh = Freshness.isFresh(value.date, now: context.date)
                HStack {
                    Circle().fill(fresh ? Color.green : Color.orange).frame(width: 6, height: 6)
                    Text(fresh ? (store.local ? "Live · this Mac" : "Live · remote read-only") : "Stale · refresh required")
                        .font(.caption).foregroundStyle(.secondary)
                    Spacer()
                }
            }
            if let volume = value.volumes.first {
                VStack(alignment: .leading, spacing: 10) {
                    Text("AVAILABLE SPACE").font(.system(size: 10, weight: .semibold)).foregroundStyle(.secondary)
                    Text(Display.bytes(volume.availableBytes)).font(.system(size: 36, weight: .semibold, design: .rounded)).monospacedDigit()
                    ProgressView(value: volume.usedFraction).tint(volume.pressure == "low" ? .orange : .green)
                        .accessibilityLabel("Disk used").accessibilityValue("\(Int(volume.usedFraction * 100)) percent")
                    HStack {
                        Text("\(Int(volume.usedFraction * 100))% used")
                        Spacer()
                        Text("\(Display.bytes(volume.totalBytes)) total")
                    }.font(.caption).foregroundStyle(.secondary)
                    if volume.pressure == "low" { Label("Low disk space", systemImage: "exclamationmark.triangle").foregroundStyle(.orange).font(.caption) }
                }.padding(14).background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 14))
            } else { message("Capacity is unavailable. No storage savings are assumed.", symbol: "externaldrive.badge.questionmark", warning: true) }
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(value.tasks.isEmpty ? "—" : Display.bytes(value.eligibleBytes)).font(.system(size: 23, weight: .semibold, design: .rounded))
                    Text("Eligible managed scratch").font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Button("Review") { store.tab = "Review" }.buttonStyle(.bordered).disabled(value.tasks.isEmpty)
            }
            if value.tasks.isEmpty {
                message("No managed tasks enrolled yet. Native agent sessions and files stay protected.", symbol: "shield.checkered", warning: false)
            } else {
                Text("RECENT MANAGED TASKS").font(.system(size: 10, weight: .semibold)).foregroundStyle(.secondary)
                ForEach(value.tasks.prefix(3)) { task in taskRow(task) }
            }
            VStack(alignment: .leading, spacing: 4) {
                if let automatic = value.autopilot {
                    Text("Autopilot: \(automatic.enabled == true ? "enabled" : automatic.enabled == false ? "disabled" : automatic.status)")
                    Text("APS captured runs: \(automatic.enrolledRuns.map(String.init) ?? "unknown") · confirmed removed \(Display.bytes(automatic.removedBytes))")
                    Text(automatic.coverage)
                }
                if let discovery = value.discovery {
                    Text("Agent discovery: \(discovery.status) · \(discovery.recognizableCount.map(String.init) ?? "unknown") recognizable processes")
                }
                Text("Capacity checked \(Display.time(value.observedAt))")
                Text("Inventory: \(value.inventoryStatus.replacingOccurrences(of: "_", with: " "))")
                if value.inventoryObservedAt != nil {
                    Text("\(Display.bytes(value.inventoryBytes)) observed · \(value.inventoryComplete ? "selected scope complete" : "partial lower bound")")
                    Text("Inventory measured \(Display.time(value.inventoryObservedAt))")
                }
            }.font(.system(size: 10)).foregroundStyle(.secondary)
        } else if store.error == nil {
            message("Reading this Mac’s real disk data…", symbol: "externaldrive", warning: false)
        }
    }

    @ViewBuilder private var review: some View {
        if !store.local { message("Remote hosts are read-only. Review and clean on the owning Mac.", symbol: "lock.shield", warning: false) }
        if let item = store.review {
            Text(item.label).font(.headline)
            Text(Display.bytes(item.candidateBytes)).font(.system(size: 30, weight: .semibold, design: .rounded))
            Text("\(item.candidateCount) eligible objects · \(item.retainedCount) retained").font(.caption).foregroundStyle(.secondary)
            if !item.reasons.isEmpty { Text("Kept: " + item.reasons.joined(separator: ", ").replacingOccurrences(of: "_", with: " ")).font(.caption).foregroundStyle(.secondary) }
            message("Only explicitly disposable, broker-owned scratch is included. Physical reclaimed space is unknown.", symbol: "lock.shield", warning: false)
            TimelineView(.periodic(from: .now, by: 1)) { _ in
                Button(store.canConfirm ? "Review & Clean…" : "No eligible files or review expired") { store.confirmationPresented = true }
                    .buttonStyle(.borderedProminent).tint(.green).disabled(!store.canConfirm)
            }
            Button("Discard preview") { store.discardReview() }.buttonStyle(.borderless)
        } else if let value = store.snapshot, !value.tasks.isEmpty {
            Text("Choose a managed task").font(.headline)
            Text("A fresh preview never deletes anything.").font(.caption).foregroundStyle(.secondary)
            ForEach(value.tasks) { task in
                VStack(alignment: .leading, spacing: 8) {
                    taskRow(task)
                    Button("Preview exact files") { store.prepare(task) }
                        .disabled(!store.local || store.busy || !Freshness.isFresh(value.date)).buttonStyle(.bordered)
                }.padding(.vertical, 4)
            }
        } else {
            message("Nothing is enrolled for review. APS will not guess which native files are disposable.", symbol: "tray", warning: false)
        }
        Text("Always protected: source code, worktrees, resumable sessions, models, original media and downloads.")
            .font(.caption).foregroundStyle(.secondary)
    }

    @ViewBuilder private var history: some View {
        Text("Scoop History").font(.headline)
        if let count = store.snapshot?.unresolvedReceipts, count > 0 {
            message("\(count) cleanup attempt(s) need inspection. Their outcomes are uncertain; do not retry them until owner receipts are reconciled.", symbol: "exclamationmark.triangle", warning: true)
        }
        if let entries = store.snapshot?.history, !entries.isEmpty {
            ForEach(entries) { item in
                VStack(alignment: .leading, spacing: 5) {
                    Text(item.label).fontWeight(.medium)
                    Text("\(item.status) · \(Display.bytes(item.removedBytes)) removed").font(.caption)
                    Text(Display.time(item.observedAt)).font(.caption2).foregroundStyle(.secondary)
                }.padding(12).frame(maxWidth: .infinity, alignment: .leading)
                    .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 10))
            }
        } else { message("No confirmed menu-bar cleanup receipts yet. No cleanup or savings are invented.", symbol: "clock", warning: false) }
        Text("History is cumulative file-removal evidence, not proof of physical SSD capacity reclaimed.").font(.caption).foregroundStyle(.secondary)
    }

    private var settings: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Settings").font(.headline)
            Toggle("Launch at login", isOn: Binding(get: { store.loginEnabled }, set: { store.setLogin($0) }))
            if let note = store.loginNote { Text(note).font(.caption).foregroundStyle(.secondary) }
            Divider()
            Label("Observe first. Confirm before cleaning.", systemImage: "lock.shield").font(.callout)
            Text("Disk capacity refreshes every minute. Existing APS monitoring handles bounded inventory and low-space alerts. No whole-disk scan runs when you open this panel.").font(.caption).foregroundStyle(.secondary)
            Button("Show integration configuration") { store.openConfiguration() }.buttonStyle(.bordered)
            Text("Enroll only APS-managed stores. Optional SSH peers must already be authenticated; host verification is never bypassed. Native Claude, Codex and AO sessions are not automatically enrolled.").font(.caption).foregroundStyle(.secondary)
            Divider()
            Text("Local preview · macOS 13+\nNo analytics. No local HTTP server.").font(.caption).foregroundStyle(.secondary)
        }
    }

    private func taskRow(_ task: ManagedTask) -> some View {
        HStack(alignment: .top) {
            Image(systemName: task.status == "completed" ? "checkmark.circle" : "lock.shield").foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 3) {
                Text(task.label).font(.callout).lineLimit(2)
                Text("\(task.status.capitalized) · \(Display.bytes(task.eligibleBytes)) eligible").font(.caption).foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
        }
    }

    private func message(_ text: String, symbol: String, warning: Bool) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: symbol).foregroundStyle(warning ? Color.orange : Color.secondary)
            Text(text).font(.caption).fixedSize(horizontal: false, vertical: true)
        }.padding(12).frame(maxWidth: .infinity, alignment: .leading)
            .background((warning ? Color.orange : Color.secondary).opacity(0.08), in: RoundedRectangle(cornerRadius: 10))
    }
}
