// Agent Deck menu bar status: shows the collector's health from status.json only.
// It never reads Claude/Codex sources and never contacts the BUSY Bar.
// Build: xcrun swiftc -swift-version 5 -O -o AgentDeckMenu AgentDeckMenu.swift
// Headless check: AgentDeckMenu --state <status.json> prints the level and exits.
import AppKit
import Foundation

let staleAfter: TimeInterval = 10
let pollInterval: TimeInterval = 5
let levels: Set<String> = ["degraded", "update", "paused", "ok"]
let levelTitles = ["stopped": "Stopped", "degraded": "Degraded", "update": "Update available",
                   "paused": "Paused", "ok": "Running"]
// Problems change the icon's shape so they stand out; "ok" is the drawn bar below.
let levelSymbols = ["stopped": "xmark.circle", "degraded": "exclamationmark.triangle",
                    "update": "arrow.clockwise.circle", "paused": "pause.circle"]
let stateOrder: [(state: String, label: String)] = [("waiting", "waiting"), ("working", "working"),
                                                     ("ready", "✓"), ("unknown", "?"), ("idle", "idle")]

struct Snapshot {
    var level = "stopped"
    var reasons: [String] = []
    var startedAt: Double?
    var sessions: [(provider: String, state: String)] = []
    var tokensError = false
    var paused = false
    var codeChanged = false

    static func stopped(_ reason: String) -> Snapshot {
        Snapshot(reasons: [reason])
    }

    static func load(_ url: URL, now: Double) -> Snapshot {
        guard let data = try? Data(contentsOf: url) else { return .stopped("Collector not running") }
        guard let status = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            return .stopped("Status file unreadable")
        }
        // A future timestamp (clock change) is as untrustworthy as an old one.
        guard let at = (status["at"] as? NSNumber)?.doubleValue, abs(now - at) <= staleAfter else {
            return .stopped("Collector not responding")
        }
        var snapshot = Snapshot()
        if let health = status["health"] as? [String: Any] {
            let level = health["level"] as? String ?? ""
            snapshot.level = levels.contains(level) ? level : "degraded"
            snapshot.reasons = health["reasons"] as? [String] ?? []
            if !levels.contains(level) { snapshot.reasons.append("Unknown collector state") }
            snapshot.codeChanged = status["code_changed"] as? Bool ?? false
        } else {
            // Without health the running collector predates the code on disk: reloading fixes it.
            snapshot.level = "update"
            snapshot.reasons = ["Collector predates health reporting"]
            snapshot.codeChanged = true
        }
        snapshot.startedAt = (status["started_at"] as? NSNumber)?.doubleValue
        snapshot.sessions = (status["sessions"] as? [[String: Any]] ?? []).compactMap { item in
            guard let provider = item["provider"] as? String, let state = item["state"] as? String else { return nil }
            return (provider, state)
        }
        snapshot.tokensError = status["tokens_error"] is String
        snapshot.paused = status["paused"] as? Bool ?? false
        return snapshot
    }

    func summary(_ provider: String) -> String {
        let states = sessions.filter { $0.provider == provider }.map(\.state)
        let parts = stateOrder.compactMap { entry -> String? in
            let count = states.filter { $0 == entry.state }.count
            return count > 0 ? "\(count) \(entry.label)" : nil
        }
        return parts.isEmpty ? "none" : parts.joined(separator: " · ")
    }
}

/// The BUSY Bar in outline with two rows of LEDs: recognizable at menu bar size.
func barImage() -> NSImage {
    let image = NSImage(size: NSSize(width: 22, height: 12), flipped: false) { rect in
        NSColor.black.set()
        let body = NSBezierPath(roundedRect: rect.insetBy(dx: 0.65, dy: 0.65), xRadius: 3, yRadius: 3)
        body.lineWidth = 1.3  // Matches SF Symbols' regular stroke.
        body.stroke()
        for row in 0..<2 {
            for column in 0..<6 {
                let led = NSRect(x: 3.4 + Double(column) * 2.75, y: 3.1 + Double(row) * 3.2, width: 1.8, height: 1.8)
                NSBezierPath(ovalIn: led).fill()
            }
        }
        return true
    }
    image.isTemplate = true
    return image
}

func icon(for level: String) -> NSImage? {
    if level == "ok" { return barImage() }
    let image = NSImage(systemSymbolName: levelSymbols[level] ?? "questionmark.circle", accessibilityDescription: nil)
    image?.isTemplate = true
    return image
}

/// Renders an icon at 2x for `--icon` previews and tests.
func writePNG(_ image: NSImage, to url: URL) -> Bool {
    let size = image.size
    guard let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size.width * 2),
                                        pixelsHigh: Int(size.height * 2), bitsPerSample: 8, samplesPerPixel: 4,
                                        hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                                        bytesPerRow: 0, bitsPerPixel: 0) else { return false }
    bitmap.size = size
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
    image.draw(in: NSRect(origin: .zero, size: size))
    NSGraphicsContext.restoreGraphicsState()
    guard let data = bitmap.representation(using: .png, properties: [:]) else { return false }
    return (try? data.write(to: url)) != nil
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    let environment = ProcessInfo.processInfo.environment
    lazy var stateDir = URL(fileURLWithPath: environment["AGENT_DECK_STATE"]
        ?? NSString(string: "~/Library/Application Support/BusybarAgentDeck").expandingTildeInPath)
    lazy var serviceLabel = environment["AGENT_DECK_LABEL"] ?? "local.busybar.agent-deck"
    let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
    let menu = NSMenu()
    var snapshot = Snapshot()
    var lastError: String?
    var timer: Timer?

    func applicationDidFinishLaunching(_ notification: Notification) {
        menu.delegate = self
        menu.autoenablesItems = false
        statusItem.menu = menu
        refresh()
        // Health only needs seconds of freshness; tolerance lets macOS coalesce the wake-up.
        let timer = Timer(timeInterval: pollInterval, target: self, selector: #selector(tick),
                          userInfo: nil, repeats: true)
        timer.tolerance = 1
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
    }

    @objc func tick() { refresh() }

    func refresh() {
        snapshot = Snapshot.load(stateDir.appendingPathComponent("status.json"), now: Date().timeIntervalSince1970)
        let title = "Agent Deck: " + (levelTitles[snapshot.level] ?? snapshot.level)
        if let image = icon(for: snapshot.level) {
            statusItem.button?.image = image
            statusItem.button?.title = ""
        } else {
            statusItem.button?.image = nil
            statusItem.button?.title = "AD"
        }
        statusItem.button?.toolTip = title
        statusItem.button?.setAccessibilityLabel(title)
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        refresh()
        menu.removeAllItems()
        menu.addItem(label("Agent Deck — " + (levelTitles[snapshot.level] ?? snapshot.level)))
        snapshot.reasons.forEach { menu.addItem(label("  " + $0)) }
        if let startedAt = snapshot.startedAt, snapshot.level != "stopped" {
            let formatter = RelativeDateTimeFormatter()
            formatter.locale = Locale(identifier: "en")
            let started = Date(timeIntervalSince1970: startedAt)
            menu.addItem(label("Started " + formatter.localizedString(for: started, relativeTo: Date())))
        }
        if snapshot.tokensError { menu.addItem(label("Token counts unavailable")) }
        if snapshot.level != "stopped" {
            menu.addItem(.separator())
            menu.addItem(label("Claude: " + snapshot.summary("claude")))
            menu.addItem(label("Codex: " + snapshot.summary("codex")))
        }
        menu.addItem(.separator())
        if snapshot.codeChanged {
            menu.addItem(action("New version on disk — Reload collector", #selector(reload)))
        }
        let acknowledge = action("Acknowledge results", #selector(acknowledge))
        acknowledge.isEnabled = snapshot.level != "stopped" && !snapshot.paused
            && snapshot.sessions.contains { $0.state == "ready" }
        menu.addItem(acknowledge)
        menu.addItem(action("Reload collector", #selector(reload)))
        menu.addItem(action("Open log", #selector(openLog)))
        if let lastError = lastError { menu.addItem(label(lastError)) }
        menu.addItem(.separator())
        menu.addItem(action("Quit menu icon", #selector(quit)))
    }

    func label(_ title: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.isEnabled = false
        return item
    }

    func action(_ title: String, _ selector: Selector) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: selector, keyEquivalent: "")
        item.target = self
        return item
    }

    @objc func acknowledge() {
        guard let python = environment["AGENT_DECK_PYTHON"] else {
            lastError = "Last action failed: Acknowledge results (no interpreter configured)"
            return
        }
        run(python, ["-I", "-m", "agentdeck.cli", "ack"], name: "Acknowledge results")
    }

    @objc func reload() {
        run("/bin/launchctl", ["kickstart", "-k", "gui/\(getuid())/\(serviceLabel)"], name: "Reload collector")
    }

    @objc func openLog() {
        if !NSWorkspace.shared.open(stateDir.appendingPathComponent("service.log")) {
            lastError = "Last action failed: Open log"
        }
    }

    @objc func quit() {
        NSApp.terminate(nil)  // Exit status 0: launchd leaves the icon closed until next login.
    }

    func run(_ executable: String, _ arguments: [String], name: String) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = arguments
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        process.terminationHandler = { [weak self] finished in
            let status = finished.terminationStatus
            DispatchQueue.main.async {
                self?.lastError = status == 0 ? nil : "Last action failed: \(name)"
                self?.refresh()
            }
        }
        do {
            try process.run()
        } catch {
            lastError = "Last action failed: \(name)"
        }
    }
}

let arguments = CommandLine.arguments
if arguments.count == 3 && arguments[1] == "--state" {
    print(Snapshot.load(URL(fileURLWithPath: arguments[2]), now: Date().timeIntervalSince1970).level)
    exit(0)
}
if arguments.count == 4 && arguments[1] == "--icon" {
    guard let image = icon(for: arguments[2]), writePNG(image, to: URL(fileURLWithPath: arguments[3])) else { exit(1) }
    exit(0)
}
let app = NSApplication.shared
let delegate = AppDelegate()  // NSApplication holds its delegate weakly.
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
