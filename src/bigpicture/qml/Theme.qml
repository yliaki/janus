pragma Singleton
import QtQuick

// Colors and a resolution-independent size unit (1080p = 1.0).
QtObject {
    property real s: 1
    function px(v) { return Math.round(v * s) }

    readonly property color bg: "#0d1015"
    readonly property color panel: "#161a21"
    readonly property color panel2: "#20262f"
    readonly property color line: "#2c3440"
    readonly property color text: "#e9edf3"
    readonly property color dim: "#8b95a7"
    readonly property color accent: "#5b9bff"
    readonly property color danger: "#ff5f5f"
    readonly property color warning: "#ffb547"

    // Player 1..8 — the same colors appear on tiles, pad pills and status rows.
    readonly property var players: ["#5b9bff", "#ff6b6b", "#3ddc97", "#ffc94a",
                                    "#c084fc", "#2dd4e0", "#ff9f43", "#f472b6"]
}
