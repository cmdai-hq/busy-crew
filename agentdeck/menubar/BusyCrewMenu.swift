// Busy Crew menu bar status: shows the collector's health from status.json only.
// It never reads Claude/Codex sources and never contacts the BUSY Bar.
// Build: xcrun swiftc -swift-version 5 -O -o BusyCrewMenu BusyCrewMenu.swift
// Headless check: BusyCrewMenu --state <status.json> prints the level and exits.
import AppKit
import Foundation

let staleAfter: TimeInterval = 10
let acknowledgeAfter: TimeInterval = 5  // Same window as cli.current_status().
let pollInterval: TimeInterval = 5
let levels: Set<String> = ["degraded", "update", "paused", "ok"]
let levelTitles = ["stopped": "Stopped", "degraded": "Degraded", "update": "Update available",
                   "paused": "Paused", "ok": "Running"]
// Problems change the icon's shape so they stand out; "ok" is the drawn duo below.
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
    var codeChanged = false
    var menuChanged = false
    var canAcknowledge = false
    var acknowledgeable = 0
    var tokens: [String: Int] = [:]
    var tokensAt: Double?
    var latestVersion: String?
    var installMode: String?

    static func stopped(_ reason: String) -> Snapshot {
        Snapshot(reasons: [reason])
    }

    static func load(_ url: URL, now: Double) -> Snapshot {
        guard let data = try? Data(contentsOf: url) else { return .stopped("Collector not running") }
        guard let status = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            return .stopped("Status file unreadable")
        }
        // A future timestamp (clock change) is as untrustworthy as an old one.
        guard let at = (status["at"] as? NSNumber)?.doubleValue, at.isFinite,
              0 <= now - at, now - at <= staleAfter else {
            return .stopped("Collector not responding")
        }
        var snapshot = Snapshot()
        snapshot.canAcknowledge = now - at <= acknowledgeAfter
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
        snapshot.acknowledgeable = (status["acknowledgeable"] as? NSNumber)?.intValue ?? 0
        snapshot.tokens = (status["tokens"] as? [String: Any] ?? [:]).compactMapValues { ($0 as? NSNumber)?.intValue }
        snapshot.tokensAt = (status["tokens_at"] as? NSNumber)?.doubleValue
        snapshot.latestVersion = status["latest_version"] as? String
        snapshot.installMode = status["install_mode"] as? String
        if let current = status["menu_fingerprint"] as? String {
            snapshot.menuChanged = current != ProcessInfo.processInfo.environment["AGENT_DECK_MENU_FINGERPRINT"]
            if snapshot.menuChanged {
                snapshot.reasons.append("Menu app changed — run `busy-crew menubar install`")
                if ["ok", "paused"].contains(snapshot.level) { snapshot.level = "update" }
            }
        }
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

/// Same compact form as the bar's token view (agentdeck/render.py compact()).
func compact(_ count: Int?) -> String {
    guard let count = count, count >= 0 else { return "?" }
    for (unit, divisor) in [("T", 1_000_000_000_000), ("G", 1_000_000_000), ("M", 1_000_000)] where count >= divisor {
        if count < 100 * divisor {
            let tenths = (count * 10 + divisor / 2) / divisor
            return "\(tenths / 10).\(tenths % 10)\(unit)"
        }
        return "\(count / divisor)\(unit)"
    }
    return count >= 1000 ? "\(count / 1000)K" : "\(count)"
}

struct Entry {
    var title = ""
    var action: Selector?
    var enabled = false
    var isSeparator = false
    static let separator = Entry(isSeparator: true)
}

/// The whole menu as data, shared by the status item and the headless `--menu` mode.
func menuEntries(_ snapshot: Snapshot, lastError: String?, now: Date, updating: Bool = false) -> [Entry] {
    var entries = [Entry(title: "Busy Crew — " + (levelTitles[snapshot.level] ?? snapshot.level))]
    entries += snapshot.reasons.map { Entry(title: "  " + $0) }
    if let startedAt = snapshot.startedAt, snapshot.level != "stopped" {
        let formatter = RelativeDateTimeFormatter()
        formatter.locale = Locale(identifier: "en")
        formatter.dateTimeStyle = .named  // "now" rather than "in 0 seconds".
        let started = Date(timeIntervalSince1970: startedAt)
        entries.append(Entry(title: "Started " + formatter.localizedString(for: started, relativeTo: now)))
    }
    if snapshot.tokensError { entries.append(Entry(title: "Token counts unavailable")) }
    if snapshot.level != "stopped" {
        entries += [.separator, Entry(title: "Claude: " + snapshot.summary("claude")),
                    Entry(title: "Codex: " + snapshot.summary("codex"))]
        if !snapshot.tokens.isEmpty {
            var line = "Tokens today: Claude \(compact(snapshot.tokens["claude"])) · Codex \(compact(snapshot.tokens["codex"]))"
            if let tokensAt = snapshot.tokensAt {
                let formatter = RelativeDateTimeFormatter()
                formatter.locale = Locale(identifier: "en")
        formatter.dateTimeStyle = .named  // "now" rather than "in 0 seconds".
                line += " (updated " + formatter.localizedString(for: Date(timeIntervalSince1970: tokensAt), relativeTo: now) + ")"
            }
            entries.append(Entry(title: line))
        }
    }
    entries.append(.separator)
    if let latest = snapshot.latestVersion, snapshot.level != "stopped" {
        if updating {
            entries.append(Entry(title: "Updating to \(latest)…"))
        } else if ["homebrew", "uv"].contains(snapshot.installMode ?? "") {
            // `busy-crew upgrade` runs the package manager, then reinstalls with the new version.
            entries.append(Entry(title: "Update to \(latest)", action: #selector(AppDelegate.update), enabled: true))
        } else if snapshot.installMode == "checkout" {
            entries.append(Entry(title: "To update: git pull --ff-only && uv sync --locked, then uv run busy-crew install"))
        }
    }
    if snapshot.codeChanged {
        entries.append(Entry(title: "New version on disk — Reload collector", action: #selector(AppDelegate.reload),
                             enabled: true))
    }
    // The collector publishes what OK would acknowledge on the bar right now.
    entries.append(Entry(title: "Acknowledge results", action: #selector(AppDelegate.acknowledge),
                         enabled: snapshot.level != "stopped" && snapshot.canAcknowledge && snapshot.acknowledgeable > 0))
    entries.append(Entry(title: "Refresh tokens now", action: #selector(AppDelegate.refreshTokens),
                         enabled: snapshot.level != "stopped"))
    entries.append(Entry(title: "Reload collector", action: #selector(AppDelegate.reload), enabled: true))
    entries.append(Entry(title: "Open log", action: #selector(AppDelegate.openLog), enabled: true))
    if let lastError = lastError { entries.append(Entry(title: lastError)) }
    entries += [.separator, Entry(title: "Quit menu icon", action: #selector(AppDelegate.quit), enabled: true)]
    return entries
}

/// Busy Crew's mark, the duo: a rounded square split into Claude's and Codex's faces, two eyes each.
/// The site draws the same shape (busy-crew-landing's MenuIcon.astro).
func duoImage() -> NSImage {
    let image = NSImage(size: NSSize(width: 16, height: 16), flipped: false) { rect in
        NSColor.black.set()
        let body = NSBezierPath(roundedRect: rect.insetBy(dx: 0.65, dy: 0.65), xRadius: 3.6, yRadius: 3.6)
        body.lineWidth = 1.3  // Matches SF Symbols' regular stroke.
        body.stroke()
        let divider = NSBezierPath()
        divider.move(to: NSPoint(x: 8, y: 0.65))
        divider.line(to: NSPoint(x: 8, y: 15.35))
        divider.lineWidth = 1.3
        divider.stroke()
        for x in [2.75, 4.75, 10.05, 12.05] {
            NSBezierPath(roundedRect: NSRect(x: x, y: 6.4, width: 1.2, height: 3.2), xRadius: 0.6, yRadius: 0.6).fill()
        }
        return true
    }
    image.isTemplate = true
    return image
}

func icon(for level: String) -> NSImage? {
    if level == "ok" { return duoImage() }
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
    // The installer always sets AGENT_DECK_STATE; the fallback mirrors storage.default_state().
    lazy var stateDir = URL(fileURLWithPath: environment["AGENT_DECK_STATE"] ?? {
        let legacy = NSString(string: "~/Library/Application Support/BusybarAgentDeck").expandingTildeInPath
        var isDirectory: ObjCBool = false
        return FileManager.default.fileExists(atPath: legacy, isDirectory: &isDirectory) && isDirectory.boolValue
            ? legacy : NSString(string: "~/Library/Application Support/BusyCrew").expandingTildeInPath
    }())
    lazy var serviceLabel = environment["AGENT_DECK_LABEL"] ?? "local.busybar.agent-deck"
    let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
    let menu = NSMenu()
    var snapshot = Snapshot()
    var lastError: String?
    var updating = false
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
        let title = "Busy Crew: " + (levelTitles[snapshot.level] ?? snapshot.level)
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
        for entry in menuEntries(snapshot, lastError: lastError, now: Date(), updating: updating) {
            if entry.isSeparator {
                menu.addItem(.separator())
                continue
            }
            let item = NSMenuItem(title: entry.title, action: entry.action, keyEquivalent: "")
            item.target = entry.action == nil ? nil : self
            item.isEnabled = entry.enabled
            menu.addItem(item)
        }
    }

    @objc func acknowledge() {
        guard let python = environment["AGENT_DECK_PYTHON"] else {
            lastError = "Last action failed: Acknowledge results (no interpreter configured)"
            return
        }
        run(python, ["-I", "-m", "agentdeck.cli", "ack"], name: "Acknowledge results")
    }

    @objc func refreshTokens() {
        guard let python = environment["AGENT_DECK_PYTHON"] else {
            lastError = "Last action failed: Refresh tokens now (no interpreter configured)"
            return
        }
        run(python, ["-I", "-m", "agentdeck.cli", "refresh"], name: "Refresh tokens now")
    }

    @objc func update() {
        guard let python = environment["AGENT_DECK_PYTHON"], !updating else { return }
        updating = true
        let name = "Update to \(snapshot.latestVersion ?? "the latest version") (see upgrade.log)"
        run(python, ["-I", "-m", "agentdeck.cli", "upgrade"], name: name) { [weak self] in
            self?.updating = false
        }
    }

    @objc func reload() {
        run("/bin/launchctl", ["kickstart", "-k", "gui/\(getuid())/\(serviceLabel)"], name: "Reload collector")
    }

    @objc func openLog() {
        lastError = NSWorkspace.shared.open(stateDir.appendingPathComponent("service.log"))
            ? nil : "Last action failed: Open log"
    }

    @objc func quit() {
        NSApp.terminate(nil)  // Exit status 0: launchd leaves the icon closed until next login.
    }

    func run(_ executable: String, _ arguments: [String], name: String, completion: (() -> Void)? = nil) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = arguments
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        process.terminationHandler = { [weak self] finished in
            let status = finished.terminationStatus
            DispatchQueue.main.async {
                self?.lastError = status == 0 ? nil : "Last action failed: \(name)"
                completion?()
                self?.refresh()
            }
        }
        do {
            try process.run()
        } catch {
            lastError = "Last action failed: \(name)"
            completion?()
        }
    }
}

let arguments = CommandLine.arguments
if arguments.count == 3 && arguments[1] == "--state" {
    print(Snapshot.load(URL(fileURLWithPath: arguments[2]), now: Date().timeIntervalSince1970).level)
    exit(0)
}
if arguments.count == 3 && arguments[1] == "--menu" {
    let snapshot = Snapshot.load(URL(fileURLWithPath: arguments[2]), now: Date().timeIntervalSince1970)
    for entry in menuEntries(snapshot, lastError: nil, now: Date()) {
        let mark = entry.action == nil ? "    " : entry.enabled ? "[x] " : "[ ] "
        print(entry.isSeparator ? "---" : mark + entry.title)
    }
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
