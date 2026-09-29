// SakethWiki -- a native window around the local dashboard.
//
// This is deliberately thin, mirroring Job Tracker's launcher
// (~/App Job Tracker/macapp/main.swift). All behaviour lives in the app
// served from 127.0.0.1:5173 (Vite) with its API on 127.0.0.1:8001; this
// bundle exists to give that page a Dock icon, an app identity, and a
// window of its own instead of a browser tab among forty others. Its only
// logic is the morning revision notification (see extension at the end);
// the only thing it stores is the date it last sent one.
//
// It does NOT own the server. launch.sh starts and restarts the backend and
// frontend directly (no launchd agent for this project). The one thing this
// app does to the server is start it if it is unexpectedly down, so a Dock
// click is never a dead end.

import Cocoa
import UserNotifications
import WebKit

let dashboardURL = URL(string: "http://127.0.0.1:5173")!
let healthURL = URL(string: "http://127.0.0.1:5173")!
// Morning revision notification (see the extension at the end). Declared up
// here on purpose: main.swift top-level constants initialize in file order,
// and anything after the run loop starts would never be set (it crashed).
let revisionNotifyMinute = 8 * 60 + 30
let revisionSummaryURL = URL(string: "http://127.0.0.1:8001/revision/today?summary=1")!
let lastNotifyKey = "lastRevisionNotify"

// Baked in at build time so the installed bundle can find launch.sh
// wherever the project lives. See build_macos_app.sh.
let projectRoot = Bundle.main.object(forInfoDictionaryKey: "SWProjectRoot") as? String

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var statusLabel: NSTextField!
    private var spinner: NSProgressIndicator!
    private var overlay: NSVisualEffectView!
    private var retryButton: NSButton!
    private var hasLoaded = false
    private var revisionTimer: Timer?

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        buildWindow()
        NSApp.activate(ignoringOtherApps: true)
        start()
        startRevisionNotifications()
    }

    private func buildMenu() {
        let appName = "SakethWiki"
        let mainMenu = NSMenu()

        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About \(appName)", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide \(appName)", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "Quit \(appName)", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        mainMenu.addItem(appItem)

        let editItem = NSMenuItem()
        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = editMenu
        mainMenu.addItem(editItem)

        let viewItem = NSMenuItem()
        let viewMenu = NSMenu(title: "View")
        viewMenu.addItem(withTitle: "Reload", action: #selector(reload), keyEquivalent: "r")
        viewMenu.addItem(withTitle: "Enter Full Screen", action: #selector(NSWindow.toggleFullScreen(_:)), keyEquivalent: "f")
        viewItem.submenu = viewMenu
        mainMenu.addItem(viewItem)

        NSApp.mainMenu = mainMenu
    }

    private func buildWindow() {
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1240, height: 840),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "SakethWiki"
        window.minSize = NSSize(width: 720, height: 560)
        window.setFrameAutosaveName("SakethWikiMain")
        window.isReleasedWhenClosed = false

        let configuration = WKWebViewConfiguration()
        webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.autoresizingMask = [.width, .height]

        let content = NSView(frame: NSRect(x: 0, y: 0, width: 1240, height: 840))
        content.autoresizingMask = [.width, .height]
        webView.frame = content.bounds
        content.addSubview(webView)

        overlay = NSVisualEffectView(frame: content.bounds)
        overlay.autoresizingMask = [.width, .height]
        overlay.material = .windowBackground
        overlay.blendingMode = .behindWindow

        spinner = NSProgressIndicator()
        spinner.style = .spinning
        spinner.controlSize = .small
        spinner.startAnimation(nil)

        statusLabel = NSTextField(labelWithString: "Starting SakethWiki…")
        statusLabel.alignment = .center
        statusLabel.textColor = .secondaryLabelColor
        statusLabel.font = .systemFont(ofSize: 13)

        retryButton = NSButton(title: "Try again", target: self, action: #selector(retry))
        retryButton.bezelStyle = .rounded
        retryButton.isHidden = true

        let stack = NSStackView(views: [spinner, statusLabel, retryButton])
        stack.orientation = .vertical
        stack.alignment = .centerX
        stack.spacing = 14
        stack.translatesAutoresizingMaskIntoConstraints = false
        overlay.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: overlay.centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: overlay.centerYAnchor),
        ])

        content.addSubview(overlay)
        window.contentView = content
        window.center()
        window.makeKeyAndOrderFront(nil)
    }

    // MARK: - Startup

    private func start() {
        showOverlay(message: "Starting SakethWiki…", spinning: true, retry: false)
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            if isServerHealthy() {
                DispatchQueue.main.async { self?.loadDashboard() }
                return
            }
            startServer()
            let deadline = Date().addingTimeInterval(25)
            while Date() < deadline {
                if isServerHealthy() {
                    DispatchQueue.main.async { self?.loadDashboard() }
                    return
                }
                Thread.sleep(forTimeInterval: 0.35)
            }
            DispatchQueue.main.async {
                self?.showOverlay(
                    message: "Could not reach SakethWiki on 127.0.0.1:5173.\nCheck /tmp/sakethwiki-*.log.",
                    spinning: false,
                    retry: true
                )
            }
        }
    }

    private func loadDashboard() {
        webView.load(URLRequest(url: dashboardURL, cachePolicy: .reloadIgnoringLocalCacheData))
    }

    private func showOverlay(message: String, spinning: Bool, retry: Bool) {
        statusLabel.stringValue = message
        spinner.isHidden = !spinning
        if spinning { spinner.startAnimation(nil) } else { spinner.stopAnimation(nil) }
        retryButton.isHidden = !retry
        overlay.isHidden = false
    }

    @objc private func retry() { start() }

    @objc private func reload() {
        if hasLoaded { webView.reload() } else { start() }
    }

    // MARK: - WKNavigationDelegate

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        hasLoaded = true
        overlay.isHidden = true
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        showOverlay(message: "The dashboard failed to load.\n\(error.localizedDescription)", spinning: false, retry: true)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        showOverlay(message: "The dashboard failed to load.\n\(error.localizedDescription)", spinning: false, retry: true)
    }

    func webView(_ webView: WKWebView,
                 decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.allow)
            return
        }
        if url.host == "127.0.0.1" || url.host == "localhost" || url.scheme == "about" {
            decisionHandler(.allow)
        } else {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
        }
    }

    func webView(_ webView: WKWebView,
                 createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    // WKWebView returns false for window.confirm() unless the UI delegate shows a panel.
    func webView(_ webView: WKWebView,
                 runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        alert.beginSheetModal(for: window) { response in
            completionHandler(response == .alertFirstButtonReturn)
        }
    }

    // MARK: - Lifecycle

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { window.makeKeyAndOrderFront(nil) }
        return true
    }
}

// MARK: - Server

func isServerHealthy() -> Bool {
    var request = URLRequest(url: healthURL)
    request.timeoutInterval = 2
    request.httpMethod = "GET"
    let semaphore = DispatchSemaphore(value: 0)
    var healthy = false
    URLSession.shared.dataTask(with: request) { _, response, _ in
        if let http = response as? HTTPURLResponse, http.statusCode == 200 { healthy = true }
        semaphore.signal()
    }.resume()
    _ = semaphore.wait(timeout: .now() + 3)
    return healthy
}

/// Best-effort start. launch.sh is the normal owner of the process; this
/// only matters when the backend/frontend are not already running.
func startServer() {
    guard let root = projectRoot else { return }
    let script = "\(root)/launch.sh"
    guard FileManager.default.isExecutableFile(atPath: script) else { return }

    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/bin/bash")
    // launch.sh ends by calling `open` on the dashboard URL, which would
    // race a browser window against this one. SW_NO_OPEN suppresses it.
    process.arguments = ["-c", "SW_NO_OPEN=1 '\(script)' >/dev/null 2>&1"]
    process.currentDirectoryURL = URL(fileURLWithPath: root)
    try? process.run()
}

// MARK: - Entry point

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.regular)
application.run()


// MARK: - Morning revision notification
//
// Every 15 minutes: if it's past 08:30 local and nothing was sent today, ask
// the backend for today's summary and post one notification. A coarse timer
// (not an exact alarm) means a Mac asleep at 08:30 still notifies after
// waking, and an app opened later that day notifies on launch.
// See docs/daily-revision/.

extension AppDelegate: UNUserNotificationCenterDelegate {
    func startRevisionNotifications() {
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        center.requestAuthorization(options: [.alert, .sound]) { granted, error in
            if let error = error { NSLog("SakethWiki notification permission error: \(error)") }
            NSLog("SakethWiki notifications granted: \(granted)")
        }
        revisionTimer = Timer.scheduledTimer(withTimeInterval: 15 * 60, repeats: true) { [weak self] _ in
            self?.maybeNotifyRevision()
        }
        // First check shortly after launch, once the backend has had a moment.
        DispatchQueue.main.asyncAfter(deadline: .now() + 20) { [weak self] in self?.maybeNotifyRevision() }
    }

    private func todayString() -> String {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd"
        return f.string(from: Date())
    }

    func maybeNotifyRevision() {
        let now = Calendar.current.dateComponents([.hour, .minute], from: Date())
        let minuteOfDay = (now.hour ?? 0) * 60 + (now.minute ?? 0)
        let today = todayString()
        guard minuteOfDay >= revisionNotifyMinute,
              UserDefaults.standard.string(forKey: lastNotifyKey) != today else { return }

        var request = URLRequest(url: revisionSummaryURL)
        request.timeoutInterval = 30
        URLSession.shared.dataTask(with: request) { data, response, _ in
            // Never notify with empty content; the next tick retries.
            guard let data = data,
                  (response as? HTTPURLResponse)?.statusCode == 200,
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
            let count = json["item_count"] as? Int ?? 0
            let topic = (json["topic"] as? [String: Any])?["title"] as? String

            let content = UNMutableNotificationContent()
            content.title = topic.map { "Topic of the day: \($0)" } ?? "SakethWiki revision"
            content.body = count > 0 ? "\(count) revision questions ready" : "Open today's revision"
            content.sound = .default
            let note = UNNotificationRequest(identifier: "revision-\(today)", content: content, trigger: nil)
            UNUserNotificationCenter.current().add(note) { error in
                if let error = error {
                    NSLog("SakethWiki notification failed: \(error)")
                } else {
                    DispatchQueue.main.async { UserDefaults.standard.set(today, forKey: lastNotifyKey) }
                }
            }
        }.resume()
    }

    // Show the banner even when SakethWiki is the frontmost app.
    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound])
    }

    // Clicking the notification opens the Revise tab.
    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        if hasLoaded {
            // Keep the page's state; the frontend listens for hashchange.
            webView.evaluateJavaScript("window.location.hash = 'revise'", completionHandler: nil)
        } else {
            webView.load(URLRequest(url: URL(string: "http://127.0.0.1:5173/#revise")!))
        }
        completionHandler()
    }
}
