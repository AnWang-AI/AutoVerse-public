import Foundation
import OSLog
import SwiftUI

/// Generated-compatible motion values shared with Web CSS. The canonical
/// values live in `design-tokens/motion.json` and are contract-tested.
enum InteractionMotion {
    static let pressDuration = MotionTokenValues.pressDuration
    static let hoverDuration = MotionTokenValues.hoverDuration
    static let stateDuration = MotionTokenValues.stateDuration
    static let panelDuration = MotionTokenValues.panelDuration
    static let activityDuration = MotionTokenValues.activityDuration

    static let state = Animation.timingCurve(
        MotionTokenValues.stateCurve.0,
        MotionTokenValues.stateCurve.1,
        MotionTokenValues.stateCurve.2,
        MotionTokenValues.stateCurve.3,
        duration: stateDuration
    )
    static let panel = Animation.timingCurve(
        MotionTokenValues.panelCurve.0,
        MotionTokenValues.panelCurve.1,
        MotionTokenValues.panelCurve.2,
        MotionTokenValues.panelCurve.3,
        duration: panelDuration
    )
    static let activity = Animation.timingCurve(
        MotionTokenValues.activityCurve.0,
        MotionTokenValues.activityCurve.1,
        MotionTokenValues.activityCurve.2,
        MotionTokenValues.activityCurve.3,
        duration: activityDuration
    )

    static func animation(named token: String) -> Animation? {
        switch token {
        case "press": .easeOut(duration: pressDuration)
        case "hover": .easeOut(duration: hoverDuration)
        case "state": state
        case "panel": panel
        case "activity": activity
        default: nil
        }
    }
}

/// Native titlebar button feedback using the same press token as Web controls.
struct ChromeButtonMotionStyle: ButtonStyle {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .opacity(configuration.isPressed ? 0.82 : 1)
            .scaleEffect(configuration.isPressed && !reduceMotion ? 0.985 : 1)
            .animation(
                reduceMotion ? nil : .easeOut(duration: InteractionMotion.pressDuration),
                value: configuration.isPressed
            )
    }
}

enum InteractionSignposts {
    private static let signposter = OSSignposter(
        subsystem: "dev.autoverse.apple",
        category: "Interaction"
    )

    static func nativeInput(_ actionID: String) {
        signposter.emitEvent("native-input", id: signposter.makeSignpostID(), "action=\(actionID)")
    }

    static func webDispatch(_ actionID: String) {
        signposter.emitEvent("web-command-dispatch", id: signposter.makeSignpostID(), "action=\(actionID)")
    }

    static func transition(_ phase: String, actionID: String) {
        signposter.emitEvent(
            phase == "start" ? "transition-start" : "transition-settled",
            id: signposter.makeSignpostID(),
            "action=\(actionID)"
        )
    }
}
