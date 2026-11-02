import Foundation
import Darwin

enum GatewayError: LocalizedError {
    case missingEngine, invalidResponse, refused, timeout, outputLimit, remoteWrite
    var errorDescription: String? {
        switch self {
        case .missingEngine: return "The bundled APS engine is unavailable. Reinstall this preview."
        case .invalidResponse: return "APS returned incomplete or invalid data. Nothing is assumed safe to clean."
        case .refused: return "APS refused this request, or the result is uncertain. Inspect owner receipts before retrying a cleanup."
        case .timeout: return "The request timed out. This host is unavailable; a cleanup outcome may be uncertain. Do not retry an uncertain cleanup."
        case .outputLimit: return "The response exceeded the safe size limit."
        case .remoteWrite: return "Remote inspection is read-only. Clean on the owning Mac."
        }
    }
}

enum Gateway {
    static func request(_ arguments: [String], peer: Peer? = nil) async throws -> Data {
        try await Task.detached(priority: .utility) { try run(arguments, peer: peer) }.value
    }

    private static func run(_ arguments: [String], peer: Peer?) throws -> Data {
        let process = Process()
        if let peer {
            guard arguments == ["snapshot"], Peer.validAlias(peer.sshAlias) else { throw GatewayError.remoteWrite }
            process.executableURL = URL(fileURLWithPath: "/usr/bin/ssh")
            process.arguments = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=4", "-o", "ConnectionAttempts=1",
                                 "-o", "StrictHostKeyChecking=yes", "-o", "ServerAliveInterval=5",
                                 "-o", "ServerAliveCountMax=1", peer.sshAlias,
                                 "exec \"$HOME/Applications/Agent Pooper Scooper.app/Contents/Resources/aps-menu\" snapshot"]
        } else {
            guard let resource = Bundle.main.resourceURL?.appendingPathComponent("aps-menu"),
                  FileManager.default.isExecutableFile(atPath: resource.path) else { throw GatewayError.missingEngine }
            process.executableURL = resource
            process.arguments = arguments
        }
        // Do not inherit arbitrary Python module paths or shell startup hooks.
        process.environment = ["HOME": NSHomeDirectory(), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8"]
        process.standardInput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        let pipe = Pipe()
        process.standardOutput = pipe
        try process.run()
        pipe.fileHandleForWriting.closeFile()
        let handle = pipe.fileHandleForReading
        defer { try? handle.close() }
        let descriptor = handle.fileDescriptor
        let flags = fcntl(descriptor, F_GETFL)
        guard flags >= 0, fcntl(descriptor, F_SETFL, flags | O_NONBLOCK) >= 0 else {
            stop(process); throw GatewayError.invalidResponse
        }
        let deadline = ProcessInfo.processInfo.systemUptime + 25
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 16_384)
        while true {
            if ProcessInfo.processInfo.systemUptime >= deadline { stop(process); throw GatewayError.timeout }
            let count = Darwin.read(descriptor, &buffer, buffer.count)
            if count > 0 {
                data.append(contentsOf: buffer.prefix(count))
                if data.count > 1_048_576 { stop(process); throw GatewayError.outputLimit }
            } else if count == 0 {
                if !process.isRunning { break }
                Thread.sleep(forTimeInterval: 0.02)
            } else if errno == EAGAIN || errno == EINTR {
                var pollState = pollfd(fd: descriptor, events: Int16(POLLIN), revents: 0)
                _ = Darwin.poll(&pollState, 1, 50)
            } else {
                stop(process); throw GatewayError.invalidResponse
            }
        }
        guard process.terminationStatus == 0 else { throw GatewayError.refused }
        guard !data.isEmpty else { throw GatewayError.invalidResponse }
        return data
    }

    private static func stop(_ process: Process) {
        // This is only the exact child created above, never a discovered process.
        if process.isRunning { process.terminate() }
        let until = ProcessInfo.processInfo.systemUptime + 0.3
        while process.isRunning && ProcessInfo.processInfo.systemUptime < until { Thread.sleep(forTimeInterval: 0.01) }
        if process.isRunning { _ = Darwin.kill(process.processIdentifier, SIGKILL) }
    }
}
