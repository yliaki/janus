import QtQuick

// Controller-driven on-screen keyboard. Steam's own (Steam + X) is drawn by
// the Steam overlay, which can't attach to a native Wayland window, so
// Janus brings its own. D-pad moves, A presses the key, X deletes, Y shift,
// LB/RB move the text cursor; the owner handles Start (save) and B (cancel).
Item {
    id: kb
    signal typed(string text)
    signal backspace()
    signal cursorMove(int delta)
    signal done()

    property bool shift: false
    property bool symbols: false
    property int row: 0
    property int col: 0

    readonly property var letterRows: [
        ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"],
        ["q", "w", "e", "r", "t", "y", "u", "i", "o", "p"],
        ["a", "s", "d", "f", "g", "h", "j", "k", "l", "-"],
        ["z", "x", "c", "v", "b", "n", "m", ",", ".", "_"],
    ]
    readonly property var symbolRows: [
        ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"],
        ["!", "@", "#", "$", "%", "^", "&", "*", "(", ")"],
        ["/", "\\", ":", ";", "'", "\"", "=", "+", "[", "]"],
        ["~", "`", "|", "<", ">", "?", "{", "}", ",", "."],
    ]
    // Bottom row: [label, action, width in key units]
    readonly property var actionRow: [
        ["⇧ Shift", "shift", 2], [kb.symbols ? "abc" : "?123", "symbols", 2],
        ["Space", "space", 3], ["⌫", "backspace", 1.5], ["Done", "done", 1.5]
    ]
    readonly property int rowCount: 5

    readonly property real unit: Math.min(Theme.px(78), width / 10)
    readonly property real keyH: Theme.px(60)
    readonly property real gap: Theme.px(8)

    implicitHeight: rowCount * (keyH + gap)

    function charRows() { return symbols ? symbolRows : letterRows }

    // x-center of key (r, c) in key units, to keep the column when moving
    // between the character rows and the wider action keys.
    function centerOf(r, c) {
        if (r < 4) return c + 0.5
        let x = 0
        for (let i = 0; i < c; i++) x += actionRow[i][2]
        return x + actionRow[c][2] / 2
    }
    function nearestCol(r, x) {
        if (r < 4) return Math.max(0, Math.min(9, Math.floor(x)))
        let best = 0, bestD = 1e9
        for (let i = 0; i < actionRow.length; i++) {
            const d = Math.abs(centerOf(4, i) - x)
            if (d < bestD) { bestD = d; best = i }
        }
        return best
    }

    function press() {
        if (row < 4) {
            const ch = charRows()[row][col]
            typed(shift && !symbols ? ch.toUpperCase() : ch)
            return
        }
        switch (actionRow[col][1]) {
        case "shift": shift = !shift; break
        case "symbols": symbols = !symbols; break
        case "space": typed(" "); break
        case "backspace": backspace(); break
        case "done": done(); break
        }
    }

    function handle(btn) {
        switch (btn) {
        case "left":
            col = row < 4 ? (col + 9) % 10 : (col + actionRow.length - 1) % actionRow.length
            break
        case "right":
            col = row < 4 ? (col + 1) % 10 : (col + 1) % actionRow.length
            break
        case "up": case "down": {
            const x = centerOf(row, col)
            row = (row + (btn === "down" ? 1 : rowCount - 1)) % rowCount
            col = nearestCol(row, x)
            break
        }
        case "a": press(); break
        case "x": backspace(); break
        case "y": shift = !shift; break
        case "lb": cursorMove(-1); break
        case "rb": cursorMove(1); break
        default: return false
        }
        return true
    }

    function reset() { row = 1; col = 0; shift = false; symbols = false }

    Column {
        spacing: kb.gap
        anchors.horizontalCenter: parent.horizontalCenter

        Repeater {
            model: 4
            delegate: Row {
                id: charRow
                required property int index
                spacing: kb.gap
                Repeater {
                    model: kb.charRows()[charRow.index]
                    delegate: Rectangle {
                        required property var modelData
                        required property int index
                        readonly property bool sel: kb.row === charRow.index && kb.col === index
                        width: kb.unit - kb.gap
                        height: kb.keyH
                        radius: Theme.px(8)
                        color: sel ? Theme.accent : Theme.panel2
                        Text {
                            anchors.centerIn: parent
                            text: kb.shift && !kb.symbols ? parent.modelData.toUpperCase() : parent.modelData
                            color: parent.sel ? Theme.bg : Theme.text
                            font.pixelSize: Theme.px(24)
                            font.bold: parent.sel
                        }
                    }
                }
            }
        }

        Row {
            spacing: kb.gap
            Repeater {
                model: kb.actionRow
                delegate: Rectangle {
                    required property var modelData
                    required property int index
                    readonly property bool sel: kb.row === 4 && kb.col === index
                    readonly property bool lit: (modelData[1] === "shift" && kb.shift)
                    width: modelData[2] * kb.unit - kb.gap
                    height: kb.keyH
                    radius: Theme.px(8)
                    color: sel ? Theme.accent : (lit ? Qt.darker(Theme.accent, 2) : Theme.panel2)
                    Text {
                        anchors.centerIn: parent
                        text: parent.modelData[0]
                        color: parent.sel ? Theme.bg : Theme.text
                        font.pixelSize: Theme.px(20)
                        font.bold: parent.sel
                    }
                }
            }
        }
    }
}
