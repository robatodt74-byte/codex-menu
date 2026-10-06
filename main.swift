import AppKit
import SwiftUI

struct LogEntry: Decodable, Equatable, Hashable {
    let role: String
    let text: String
}

struct Session: Decodable, Identifiable {
    let id: String
    let title: String
    let cwd: String
    let path: String
    var state: String
    var detail: String
    let logs: [LogEntry]
    var label: String {
        ["running": "working", "waiting": "awaiting approval", "stopped": "no work", "unknown": "no work", "error": "error"][state] ?? "no work"
    }
    var dot: String { ["running": "●", "waiting": "◉", "stopped": "○", "error": "!"][state] ?? "?" }
    var color: Color {
        switch state {
        case "running": return .green
        case "waiting": return .orange
        case "error": return .red
        case "stopped": return .secondary
        default: return .secondary
        }
    }
}

struct Snapshot: Decodable {
    var sessions: [Session]
    let error: String?
    let hookConfigured: Bool
    let lastHook: Double
    let observedAt: Double
    let liveCheck: Bool
}

final class MonitorModel: ObservableObject {
    @Published var snapshot: Snapshot?
    @Published var error: String?
    @Published var inspected = "" {
        didSet { persistSelection() }
    }
    @Published var pinned = "" {
        didSet {
            if !preview { UserDefaults.standard.set(pinned, forKey: "pinnedSession") }
            updated?()
        }
    }
    var updated: (() -> Void)?
    let preview: Bool
    private var worker: Process?
    private var output: Pipe?
    private var lastReceived = Date()
    private var timer: Timer?
    var sessions: [Session] { snapshot?.sessions ?? [] }
    var selected: Session? { sessions.first { $0.id == inspected } }
    var pinnedSession: Session? { sessions.first { $0.id == pinned } }
    var menuTitle: String {
        let title = pinnedSession.map { String($0.title.prefix(12)) } ?? "Codex"
        if error != nil { return "! error · \(title)" }
        guard let session = pinnedSession else { return "○ no work · \(title)" }
        return "\(session.dot) \(session.label) · \(title)"
    }
    var dataURL: URL {
        if let path = ProcessInfo.processInfo.environment["CODEX_MENU_DATA"] {
            return URL(fileURLWithPath: path, isDirectory: true)
        }
        return FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/CodexMenu")
    }
    var script: URL { Bundle.main.resourceURL!.appendingPathComponent("backend.py") }

    init(preview: Bool = false) {
        self.preview = preview
        if !preview { pinned = UserDefaults.standard.string(forKey: "pinnedSession") ?? "" }
    }

    func persistSelection() {
        guard !preview, !inspected.isEmpty else { return }
        do {
            try FileManager.default.createDirectory(at: dataURL, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            let data = try JSONSerialization.data(withJSONObject: ["id": inspected])
            try data.write(to: dataURL.appendingPathComponent("selection.json"), options: .atomic)
        } catch { self.error = "表示対象の保存に失敗: \(error.localizedDescription)" }
    }

    func start() {
        stop()
        error = nil
        let process = Process()
        let pipe = Pipe()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = [script.path]
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        // Each worker owns its buffer, including callbacks queued during reconnect.
        let io = DispatchQueue(label: "local.codex-menu.reader")
        var buffer = Data()
        output = pipe
        worker = process
        pipe.fileHandleForReading.readabilityHandler = { [weak self, weak process] handle in
            let chunk = handle.availableData
            guard !chunk.isEmpty else { return }
            io.async { [weak self, weak process] in
                guard let self = self else { return }
                buffer.append(chunk)
                while let newline = buffer.firstIndex(of: 10) {
                    let line = buffer.prefix(upTo: newline)
                    buffer.removeSubrange(...newline)
                    do {
                        let snapshot = try JSONDecoder().decode(Snapshot.self, from: line)
                        DispatchQueue.main.async { [weak self, weak process] in
                            guard let self = self, self.worker === process else { return }
                            self.accept(snapshot)
                        }
                    } catch {
                        DispatchQueue.main.async { [weak self] in self?.fail("監視データを読み取れません") }
                    }
                }
                if buffer.count > 8 * 1024 * 1024 {
                    buffer.removeAll()
                    DispatchQueue.main.async { [weak self] in self?.fail("監視データが上限を超えました") }
                }
            }
        }
        process.terminationHandler = { [weak self, weak process] _ in
            DispatchQueue.main.async {
                guard let self = self, self.worker === process else { return }
                self.fail("監視プロセスが終了しました。「再接続」を押してください。")
            }
        }
        do {
            try process.run()
            lastReceived = Date()
            timer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in
                guard let self = self else { return }
                if Date().timeIntervalSince(self.lastReceived) > 15 {
                    self.fail("状態更新が止まっています。「再接続」を押してください。")
                }
            }
        } catch { fail("監視を起動できません: \(error.localizedDescription)") }
    }

    func accept(_ value: Snapshot) {
        lastReceived = Date()
        var value = value
        if let error = value.error {
            for i in value.sessions.indices {
                value.sessions[i].state = "error"
                value.sessions[i].detail = error
            }
        }
        snapshot = value
        error = value.error
        if inspected.isEmpty || !sessions.contains(where: { $0.id == inspected }) {
            inspected = pinnedSession?.id ?? sessions.first?.id ?? ""
        }
        if pinned.isEmpty { pinned = inspected }
        updated?()
    }

    func fail(_ message: String) {
        error = message
        if var value = snapshot {
            for i in value.sessions.indices {
                value.sessions[i].state = "error"
                value.sessions[i].detail = "取得が停止しています"
            }
            snapshot = value
        }
        updated?()
    }

    func stop() {
        timer?.invalidate()
        timer = nil
        let old = worker
        worker = nil
        output?.fileHandleForReading.readabilityHandler = nil
        if old?.isRunning == true { old?.terminate() }
        output = nil
    }

    func openLog() {
        guard let selected = selected else { return }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        p.arguments = ["-a", "Zed", selected.path]
        do { try p.run() } catch { self.error = "Zedでログを開けません: \(error.localizedDescription)" }
    }

    func configureHooks() {
        let alert = NSAlert()
        alert.messageText = "承認待ちの通知を追加しますか？"
        alert.informativeText = "~/.codex/hooks.jsonに、承認要求・ツール完了・処理開始/終了の監視用Hookを追加します。既存のHookを残し、日時付きバックアップを保存します。\n\n通知はこのMac内だけに保存します。承認への回答は行いません。追加後、Codexで新しいHooksの内容を確認・信頼する必要があります。\n\n実行先：\(script.path)"
        alert.addButton(withTitle: "追加する")
        alert.addButton(withTitle: "キャンセル")
        NSApp.activate(ignoringOtherApps: true)
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        let process = Process()
        let pipe = Pipe()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = [script.path, "--install-hooks"]
        process.standardOutput = pipe
        process.standardError = pipe
        do {
            try process.run()
            let output = pipe.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            let result = NSAlert()
            result.messageText = process.terminationStatus == 0 ? "追加しました。Codex側で信頼が必要です" : "追加できませんでした"
            result.informativeText = process.terminationStatus == 0
                ? "Codex CLIを開き、/hooksからCodexMenuの6イベントを確認・信頼してください。既存チャットに反映されるタイミングはCodexの設定再読込に依存します。\n\n" + String(decoding: output, as: UTF8.self)
                : String(decoding: output, as: UTF8.self)
            result.runModal()
        } catch { self.error = "設定に失敗: \(error.localizedDescription)" }
    }
}

struct Panel: View {
    @ObservedObject var model: MonitorModel
    @State private var search = ""
    @State private var includeStopped = true
    var filtered: [Session] {
        model.sessions.filter {
            (includeStopped || $0.label != "no work") &&
            (search.isEmpty || ($0.title + " " + $0.cwd).localizedCaseInsensitiveContains(search))
        }
    }
    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("Codex").font(.title2.bold())
                Spacer()
                Text("working \(model.sessions.filter { $0.state == "running" }.count) · awaiting approval \(model.sessions.filter { $0.state == "waiting" }.count)")
                    .font(.callout).foregroundColor(.secondary)
            }.padding(16)
            if let error = model.error {
                Text(error).font(.caption).foregroundColor(.red).frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 16).padding(.bottom, 8)
            }
            Divider()
            HStack(spacing: 0) {
                VStack(alignment: .leading, spacing: 8) {
                    TextField("チャット・フォルダーを検索", text: $search).textFieldStyle(.roundedBorder)
                    Toggle("no work のチャットも表示", isOn: $includeStopped).font(.caption)
                    ScrollView {
                        LazyVStack(spacing: 4) {
                            ForEach(filtered) { session in
                                Button { model.inspected = session.id } label: {
                                    HStack(alignment: .top, spacing: 8) {
                                        Text(session.dot).foregroundColor(session.color)
                                        VStack(alignment: .leading, spacing: 4) {
                                            Text(session.title).font(.system(size: 13, weight: .medium)).lineLimit(2).frame(maxWidth: .infinity, alignment: .leading)
                                            Text("\(session.label) · \(URL(fileURLWithPath: session.cwd).lastPathComponent)")
                                                .font(.caption).foregroundColor(.secondary).lineLimit(1)
                                        }
                                        if model.pinned == session.id { Text("固定").font(.caption2).foregroundColor(.secondary) }
                                    }.padding(8).background(model.inspected == session.id ? Color.accentColor.opacity(0.12) : Color.clear).cornerRadius(6)
                                }.buttonStyle(.plain).accessibilityLabel("\(session.title)、\(session.label)\(model.pinned == session.id ? "、メニューバー表示対象" : "")")
                            }
                            if filtered.isEmpty { Text("該当するチャットがありません").foregroundColor(.secondary).padding(.top, 20) }
                        }
                    }
                    Text("\(filtered.count)件 · ローカルの親チャット").font(.caption2).foregroundColor(.secondary)
                }.padding(12).frame(width: 286)
                Divider()
                VStack(alignment: .leading, spacing: 8) {
                    if let session = model.selected {
                        Text(session.title).font(.headline).lineLimit(2)
                        Text("\(session.dot) \(session.label) — \(session.detail)").font(.caption).foregroundColor(session.color)
                        HStack {
                            Button(model.pinned == session.id ? "メニューバー表示中" : "メニューバーに表示") { model.pinned = session.id }
                                .disabled(model.pinned == session.id)
                            Button("元ログをZedで開く") { model.openLog() }
                        }
                        Divider()
                        ScrollViewReader { proxy in
                            ScrollView {
                                LazyVStack(alignment: .leading, spacing: 12) {
                                    ForEach(Array(session.logs.enumerated()), id: \.offset) { index, log in
                                        VStack(alignment: .leading, spacing: 4) {
                                            Text(log.role).font(.caption.bold()).foregroundColor(.secondary)
                                            Text(log.text).font(.system(size: 12)).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                                        }.id(index)
                                    }
                                    if session.logs.isEmpty { Text("ログを読み込んでいます…").foregroundColor(.secondary).padding(.top, 16) }
                                }.padding(.trailing, 6)
                            }.task(id: session.logs) {
                                if let last = session.logs.indices.last { proxy.scrollTo(last, anchor: .bottom) }
                            }
                        }
                        Text("直近のログ抜粋 · 2秒ごとに更新").font(.caption2).foregroundColor(.secondary)
                    } else {
                        Text("チャットを選ぶとログを表示します").foregroundColor(.secondary)
                        Spacer()
                    }
                }.padding(14).frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            }
            Divider()
            HStack {
                VStack(alignment: .leading, spacing: 3) {
                    Text(model.snapshot?.hookConfigured == true ? "Hooks設定あり · 信頼・通知受信の確認が必要" : "承認待ちの検出にはHooksの設定・信頼が必要")
                    if let stamp = model.snapshot?.lastHook, stamp > 0 {
                        Text("最終Hook受信 \(Date(timeIntervalSince1970: stamp).formatted(date: .omitted, time: .standard))")
                    }
                }.font(.caption2).foregroundColor(.secondary)
                Spacer()
                Button("awaiting approval の取得を設定…") { model.configureHooks() }
                Button("再接続") { model.start() }
                Button("終了") { NSApp.terminate(nil) }
            }.padding(12)
        }.frame(width: 770, height: 590).background(Color(NSColor.windowBackgroundColor))
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    let model = MonitorModel()
    var item: NSStatusItem!
    let popover = NSPopover()
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.title = model.menuTitle
        item.button?.target = self
        item.button?.action = #selector(toggle)
        popover.behavior = .transient
        let controller = NSHostingController(rootView: Panel(model: model))
        let panelSize = NSSize(width: 770, height: 590)
        controller.view.frame = NSRect(origin: .zero, size: panelSize)
        controller.preferredContentSize = panelSize
        popover.contentViewController = controller
        popover.contentSize = panelSize
        model.updated = { [weak self] in self?.updateTitle() }
        model.start()
        if CommandLine.arguments.contains("--show") {
            DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in self?.toggle() }
        }
    }
    func updateTitle() {
        item.button?.title = model.menuTitle
        item.button?.toolTip = model.error ?? model.pinnedSession.map { "\($0.title) — \($0.detail)" } ?? "表示対象を選択してください"
        item.button?.setAccessibilityLabel(item.button?.title ?? "Codex")
        // A small local heartbeat supports verification without copying conversation data.
        if let snapshot = model.snapshot,
           let data = try? JSONSerialization.data(withJSONObject: [
                "observedAt": snapshot.observedAt, "sessionCount": snapshot.sessions.count,
                "pinned": model.pinned, "inspected": model.inspected,
                "state": model.pinnedSession?.state ?? "unknown", "popoverShown": popover.isShown,
                "error": model.error ?? "", "logCount": model.selected?.logs.count ?? 0,
                "popoverSize": [popover.contentSize.width, popover.contentSize.height],
                "hostingBounds": NSStringFromRect(popover.contentViewController?.view.bounds ?? .zero)
           ]) {
            try? data.write(to: model.dataURL.appendingPathComponent("diagnostics.json"), options: .atomic)
        }
    }
    @objc func toggle() {
        guard let button = item.button else { return }
        if popover.isShown { popover.performClose(nil) }
        else {
            if model.pinnedSession != nil { model.inspected = model.pinned }
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
            NSApp.activate(ignoringOtherApps: true)
        }
    }
    func applicationWillTerminate(_ notification: Notification) { model.stop() }
}

let app = NSApplication.shared
if CommandLine.arguments.contains("--self-test") || CommandLine.arguments.contains("--preview") {
    app.setActivationPolicy(.accessory)
    let model = MonitorModel(preview: true)
    let sample = Snapshot(sessions: [
        Session(id: "one", title: "環境開発", cwd: "/Projects/環境開発", path: "", state: "running", detail: "処理開始済み・終了記録なし", logs: [LogEntry(role: "Codex", text: "変更を確認し、テストを実行しています。")]),
        Session(id: "two", title: "Remotion", cwd: "/Projects/Remotion", path: "", state: "waiting", detail: "PermissionRequest通知を受信", logs: [LogEntry(role: "Codex", text: "コマンドの実行許可を待っています。")]),
        Session(id: "three", title: "GEF", cwd: "/Projects/GEF", path: "", state: "stopped", detail: "処理終了", logs: [LogEntry(role: "Codex", text: "検証が完了しました。")])
    ], error: nil, hookConfigured: false, lastHook: 0, observedAt: Date().timeIntervalSince1970, liveCheck: true)
    precondition(model.menuTitle == "○ no work · Codex")
    model.accept(sample)
    precondition(model.sessions.map(\.label) == ["working", "awaiting approval", "no work"])
    precondition(model.menuTitle == "● working · 環境開発")
    precondition(model.pinnedSession?.id == "one")
    model.inspected = "two"
    precondition(model.selected?.logs.first?.text == "コマンドの実行許可を待っています。")
    precondition(model.pinnedSession?.id == "one")
    model.pinned = model.inspected
    precondition(model.pinnedSession?.label == "awaiting approval")
    model.fail("test disconnection")
    precondition(model.sessions.allSatisfy { $0.state == "error" && $0.label == "error" })
    precondition(model.menuTitle == "! error · Remotion")
    model.accept(Snapshot(sessions: sample.sessions, error: "test retrieval error", hookConfigured: false, lastHook: 0, observedAt: Date().timeIntervalSince1970, liveCheck: false))
    precondition(model.menuTitle == "! error · Remotion")
    model.accept(Snapshot(sessions: [], error: "test empty retrieval error", hookConfigured: false, lastHook: 0, observedAt: Date().timeIntervalSince1970, liveCheck: false))
    precondition(model.menuTitle == "! error · Codex")
    model.accept(sample)
    precondition(model.error == nil)
    if CommandLine.arguments.contains("--preview") {
        let view = NSHostingView(rootView: Panel(model: model).environment(\.colorScheme, .light))
        let rect = NSRect(x: 0, y: 0, width: 770, height: 590)
        let window = NSWindow(contentRect: rect, styleMask: [.borderless], backing: .buffered, defer: false)
        window.appearance = NSAppearance(named: .aqua)
        window.contentView = view
        view.frame = rect
        window.display()
        RunLoop.main.run(until: Date().addingTimeInterval(0.5))
        view.layoutSubtreeIfNeeded()
        guard let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { fatalError("Cannot render preview") }
        view.cacheDisplay(in: view.bounds, to: rep)
        let target = CommandLine.arguments.last!
        try rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: target))
        print("Rendered preview: \(target)")
    }
    print("Native checks passed: selection, pinning, log switch, disconnect, reconnect")
    exit(0)
}
let delegate = AppDelegate()
app.delegate = delegate
app.run()
