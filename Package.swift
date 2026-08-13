// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "SecondHello",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "SecondHello", targets: ["SecondHello"])],
    targets: [
        .executableTarget(name: "SecondHello", resources: [.process("Resources")]),
        .testTarget(name: "SecondHelloTests", dependencies: ["SecondHello"])
    ]
)
