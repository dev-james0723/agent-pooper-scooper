// swift-tools-version: 5.9
import PackageDescription
let package = Package(name: "APSMenuBar", platforms: [.macOS(.v13)], products: [.executable(name: "APSMenuBar", targets: ["APSMenuBar"])], targets: [.executableTarget(name: "APSMenuBar"), .testTarget(name: "APSMenuBarTests", dependencies: ["APSMenuBar"])])
