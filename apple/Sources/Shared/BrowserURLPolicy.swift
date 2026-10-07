import Foundation
import Network

/// Redirect-safe network policy for the visible browser host.
///
/// The Python tool guard validates the requested URL. WebKit applies this policy
/// again to every main-frame and sub-frame navigation so a redirect cannot turn
/// a public URL into an internal-network request. Literal loopback remains
/// available for local app testing; other non-public address ranges are denied.
enum BrowserURLPolicy {
    static func isAllowed(_ url: URL) -> Bool {
        guard let scheme = url.scheme?.lowercased(), ["http", "https"].contains(scheme),
              let host = url.host?.lowercased(), !host.isEmpty else { return false }

        if host == "localhost" || host.hasSuffix(".localhost") { return true }
        if host.hasSuffix(".local") || host.hasSuffix(".internal") || host.hasSuffix(".lan") {
            return false
        }
        if host == "metadata.google.internal" { return false }

        if let address = IPv4Address(host) {
            return isAllowedIPv4(Array(address.rawValue))
        }
        if let address = IPv6Address(host) {
            return isAllowedIPv6(Array(address.rawValue))
        }

        // Reject alternate numeric IPv4 spellings such as 2130706433 and
        // 0x7f000001. They are accepted by some URL/network stacks but bypass
        // conventional dotted-quad checks.
        if host.allSatisfy({ $0.isNumber }) || host.hasPrefix("0x") {
            return false
        }
        return true
    }

    private static func isAllowedIPv4(_ bytes: [UInt8]) -> Bool {
        guard bytes.count == 4 else { return false }
        let a = bytes[0]
        let b = bytes[1]
        let c = bytes[2]
        let d = bytes[3]

        if a == 127 { return true }
        if a == 0 || a == 10 { return false }
        if a == 100 && (64...127).contains(b) { return false }
        if a == 169 && b == 254 { return false }
        if a == 172 && (16...31).contains(b) { return false }
        if a == 192 && b == 0 && c == 0 && d != 9 && d != 10 { return false }
        if a == 192 && b == 0 && c == 2 { return false }
        if a == 192 && b == 168 { return false }
        if a == 198 && (b == 18 || b == 19) { return false }
        if a == 198 && b == 51 && c == 100 { return false }
        if a == 203 && b == 0 && c == 113 { return false }
        if a >= 224 { return false }
        return true
    }

    private static func isAllowedIPv6(_ bytes: [UInt8]) -> Bool {
        guard bytes.count == 16 else { return false }
        let loopback = bytes.dropLast().allSatisfy { $0 == 0 } && bytes.last == 1
        if loopback { return true }

        let mappedPrefix = bytes.prefix(10).allSatisfy { $0 == 0 }
            && bytes[10] == 0xff && bytes[11] == 0xff
        if mappedPrefix {
            return isAllowedIPv4(Array(bytes.suffix(4)))
        }

        // Only globally routable 2000::/3 unicast is accepted. Explicitly
        // reject the documentation prefix 2001:db8::/32 within that range.
        guard bytes[0] & 0xe0 == 0x20 else { return false }
        if bytes[0] == 0x20 && bytes[1] == 0x01 && bytes[2] == 0x0d && bytes[3] == 0xb8 {
            return false
        }
        return true
    }
}
