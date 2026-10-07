import Foundation
import Testing
@testable import AutoVerseApple

@Suite("Browser URL policy")
struct BrowserURLPolicyTests {
    @Test("Resolves keywords with Google and preserves website inputs")
    func resolvesToolbarInput() throws {
        let search = try #require(
            BrowserAddressResolver.resolve("  AutoVerse browser tools  ")
        )
        #expect(search.host == "www.google.com")
        #expect(search.path == "/search")
        #expect(
            URLComponents(url: search, resolvingAgainstBaseURL: false)?
                .queryItems?.first(where: { $0.name == "q" })?.value
                == "AutoVerse browser tools"
        )
        #expect(
            BrowserAddressResolver.resolve("example.com/docs")?.absoluteString
                == "https://example.com/docs"
        )
        #expect(
            BrowserAddressResolver.resolve("http://localhost:8170")?.absoluteString
                == "http://localhost:8170"
        )
        #expect(
            BrowserAddressResolver.resolve("localhost/status")?.absoluteString
                == "https://localhost/status"
        )
        #expect(BrowserAddressResolver.resolve("   ") == nil)
    }

    @Test("Allows public HTTP(S) and all literal loopback forms", arguments: [
        "https://example.com/path",
        "http://localhost:8170",
        "http://app.localhost:3000",
        "http://127.0.0.1:8000",
        "http://127.99.1.2:8000",
        "http://2130706433",
        "http://0x7f000001",
        "https://192.0.0.9",
        "https://192.0.0.10",
        "https://192.31.196.1",
        "https://198.51.1.1",
        "https://203.0.114.1",
        "http://[::1]:8000",
        "https://[2606:4700:4700::1111]/",
    ])
    func allowsExpectedURLs(_ raw: String) {
        #expect(BrowserURLPolicy.isAllowed(URL(string: raw)!))
    }

    @Test("Blocks non-web schemes, private, metadata and reserved ranges", arguments: [
        "file:///etc/passwd",
        "ftp://example.com/file",
        "http://10.1.2.3",
        "http://100.64.0.1",
        "http://169.254.169.254/latest/meta-data",
        "http://172.16.0.1",
        "http://172.31.255.255",
        "http://192.168.1.1",
        "http://192.0.0.1",
        "http://192.0.2.1",
        "http://198.18.0.1",
        "http://198.51.100.1",
        "http://203.0.113.1",
        "http://224.0.0.1",
        "http://255.255.255.255",
        "http://[fc00::1]",
        "http://[fe80::1]",
        "http://[2001:db8::1]",
        "http://printer.local",
        "http://metadata.google.internal",
        "http://167772161",
        "http://0x0a000001",
    ])
    func blocksUnsafeURLs(_ raw: String) {
        #expect(!BrowserURLPolicy.isAllowed(URL(string: raw)!))
    }
}
