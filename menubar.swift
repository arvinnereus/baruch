// Baruch menu-bar companion — system-wide recording status + control.
// Native NSStatusItem: red dot + live timer while recording, visible in any
// app. Talks to the Baruch server's REST API on localhost.
// Build: swiftc -O menubar.swift -o menubar   (done by run.sh)

import AppKit
import Foundation

let BASE = "http://127.0.0.1:8377"

func api(_ path: String, method: String = "GET", body: [String: Any]? = nil,
         done: @escaping (Any?) -> Void) {
    guard let url = URL(string: BASE + path) else { done(nil); return }
    var req = URLRequest(url: url)
    req.httpMethod = method
    req.timeoutInterval = 3
    if let body = body {
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
    }
    URLSession.shared.dataTask(with: req) { data, _, _ in
        let obj = data.flatMap { try? JSONSerialization.jsonObject(with: $0) }
        DispatchQueue.main.async { done(obj) }
    }.resume()
}

class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    var item: NSStatusItem!
    var rec: [String: Any]?
    var serverUp = false

    func applicationDidFinishLaunching(_ n: Notification) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.image = NSImage(systemSymbolName: "mic",
                                     accessibilityDescription: "Baruch")
        let menu = NSMenu()
        menu.delegate = self
        item.menu = menu
        poll()
        Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { _ in
            if self.rec != nil { self.render() }
        }
        Timer.scheduledTimer(withTimeInterval: 4.0, repeats: true) { _ in
            self.poll()
        }
    }

    func poll() {
        api("/api/meetings") { obj in
            if let arr = obj as? [[String: Any]] {
                self.serverUp = true
                self.rec = arr.first {
                    let s = $0["status"] as? String
                    return s == "recording" || s == "paused"
                }
            } else {
                self.serverUp = false
                self.rec = nil
            }
            self.render()
        }
    }

    func elapsed() -> Int {
        guard let r = rec else { return 0 }
        let base = r["recorded_s"] as? Int ?? 0
        if (r["status"] as? String) == "recording",
           let st = r["record_started_at"] as? Int {
            return base + Int(Date().timeIntervalSince1970) - st
        }
        return base
    }

    func render() {
        guard let btn = item.button else { return }
        if let r = rec {
            let paused = (r["status"] as? String) == "paused"
            let t = elapsed()
            btn.image = NSImage(systemSymbolName:
                paused ? "pause.circle.fill" : "record.circle.fill",
                accessibilityDescription: paused ? "Paused" : "Recording")
            btn.contentTintColor = paused ? .systemOrange : .systemRed
            btn.title = String(format: " %d:%02d", t / 60, t % 60)
        } else {
            btn.image = NSImage(systemSymbolName: serverUp ? "mic" : "mic.slash",
                                accessibilityDescription: "Baruch")
            btn.contentTintColor = nil
            btn.title = ""
        }
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        if let r = rec {
            let paused = (r["status"] as? String) == "paused"
            let title = (r["title"] as? String) ?? "Recording"
            menu.addItem(withTitle: (paused ? "⏸ " : "● ") + title,
                         action: nil, keyEquivalent: "")
            if paused {
                menu.addItem(withTitle: "Resume recording",
                             action: #selector(resumeRec), keyEquivalent: "r")
                    .target = self
            } else {
                menu.addItem(withTitle: "Pause recording",
                             action: #selector(pauseRec), keyEquivalent: "p")
                    .target = self
            }
            menu.addItem(withTitle: "Stop & make notes",
                         action: #selector(stopRec), keyEquivalent: "s")
                .target = self
        } else if serverUp {
            menu.addItem(withTitle: "Start in-person recording",
                         action: #selector(startInperson), keyEquivalent: "i")
                .target = self
            menu.addItem(withTitle: "Start online recording (Zoom/Meet)",
                         action: #selector(startOnline), keyEquivalent: "o")
                .target = self
        } else {
            menu.addItem(withTitle: "Baruch server is not running",
                         action: nil, keyEquivalent: "")
            menu.addItem(withTitle: "Start Baruch",
                         action: #selector(startServer), keyEquivalent: "l")
                .target = self
        }
        menu.addItem(NSMenuItem.separator())
        menu.addItem(withTitle: "Open Baruch",
                     action: #selector(openApp), keyEquivalent: "")
            .target = self
        menu.addItem(NSMenuItem.separator())
        menu.addItem(withTitle: "Quit menu bar",
                     action: #selector(NSApplication.terminate(_:)),
                     keyEquivalent: "q")
    }

    @objc func startInperson() { quickstart("inperson") }
    @objc func startOnline() { quickstart("online") }
    func quickstart(_ mode: String) {
        api("/api/quickstart", method: "POST", body: ["mode": mode]) { _ in
            self.poll()
        }
    }
    @objc func pauseRec() { act("pause") }
    @objc func resumeRec() { act("resume") }
    @objc func stopRec() { act("stop") }
    func act(_ a: String) {
        guard let id = rec?["id"] as? String else { return }
        api("/api/meetings/\(id)/\(a)", method: "POST", body: [:]) { _ in
            self.poll()
        }
    }
    @objc func openApp() { NSWorkspace.shared.open(URL(string: BASE)!) }
    @objc func startServer() {
        let dir = URL(fileURLWithPath: CommandLine.arguments[0])
            .deletingLastPathComponent().path
        let p = Process()
        p.launchPath = "/bin/bash"
        p.arguments = ["-c",
            "cd '\(dir)' && nohup ./run.sh > server-launch.log 2>&1 &"]
        try? p.run()
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
