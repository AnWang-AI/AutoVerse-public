// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "AutoVerseApple",
    platforms: [
        .macOS(.v14),
        .iOS(.v17),
    ],
    products: [
        .executable(name: "AutoVerseApple", targets: ["AutoVerseApple"]),
    ],
    targets: [
        .executableTarget(
            name: "AutoVerseApple",
            path: "Sources"
        ),
        .testTarget(
            name: "AutoVerseAppleTests",
            dependencies: ["AutoVerseApple"],
            path: "Tests"
        ),
    ]
)
