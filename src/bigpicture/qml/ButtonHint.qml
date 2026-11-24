import QtQuick

// A controller glyph plus its action, e.g. (A) Join. With the mouse/keyboard
// (inputMode.mouse) it turns into a clickable button that sends the same action
// (inputMode.press), labelled with its keyboard key; pad-only hints (D-pad
// moves, per-pad actions) hide, since the mouse does those directly.
Item {
    id: root
    property string btn: "a"
    property string label: ""
    property string key: keys[btn] || ""    // shown on the button; override where it differs
    property bool padOnly: ["dpad", "updown", "leftright", "lbrb", "ltrt"].indexOf(btn) >= 0

    readonly property var faceColors: ({ a: "#3ddc97", b: "#ff6b6b", x: "#5b9bff", y: "#ffc94a" })
    readonly property bool face: faceColors[btn] !== undefined
    readonly property var glyphs: ({ start: "Start", back: "Back", lb: "LB", rb: "RB",
                                     lt: "LT", rt: "RT", lbrb: "LB  RB", ltrt: "LT  RT",
                                     dpad: "✚", leftright: "◀ ▶", updown: "▲ ▼" })
    // Main.qml's keyboard map, the other way round.
    readonly property var keys: ({ a: "Enter", b: "Esc", x: "X", y: "Y", start: "Space",
                                   back: "F5", lb: "PgUp", rb: "PgDn", lt: "[", rt: "]" })
    // A clickable button even with a pad in hand: for actions no pad can
    // send right now (the controller setup wizard with a single pad).
    property bool forceButton: false
    readonly property bool asButton: (inputMode.mouse || forceButton) && label !== ""

    visible: !((inputMode.mouse || forceButton) && padOnly)
    implicitWidth: asButton ? button.width : hint.implicitWidth
    implicitHeight: Theme.px(44)
    width: implicitWidth
    height: implicitHeight

    Row {
        id: hint
        visible: !root.asButton
        anchors.verticalCenter: parent.verticalCenter
        spacing: Theme.px(10)

        Rectangle {
            anchors.verticalCenter: parent.verticalCenter
            height: Theme.px(34)
            width: root.face ? height : glyph.implicitWidth + Theme.px(22)
            radius: root.face ? height / 2 : Theme.px(8)
            color: root.face ? "transparent" : Theme.panel2
            border.width: root.face ? Theme.px(2) : 0
            border.color: root.face ? root.faceColors[root.btn] : "transparent"
            Text {
                id: glyph
                anchors.centerIn: parent
                text: root.face ? root.btn.toUpperCase() : (root.glyphs[root.btn] || root.btn)
                color: root.face ? root.faceColors[root.btn] : Theme.text
                font.pixelSize: Theme.px(17)
                font.bold: true
            }
        }
        Text {
            anchors.verticalCenter: parent.verticalCenter
            text: root.label
            color: Theme.dim
            font.pixelSize: Theme.px(20)
        }
    }

    Rectangle {
        id: button
        visible: root.asButton
        anchors.verticalCenter: parent.verticalCenter
        height: Theme.px(44)
        width: buttonRow.implicitWidth + Theme.px(32)
        radius: Theme.px(10)
        color: area.pressed ? Theme.line : area.containsMouse ? Theme.panel2 : Theme.panel
        border.color: area.containsMouse ? Theme.accent : Theme.line
        border.width: Theme.px(2)
        Row {
            id: buttonRow
            anchors.centerIn: parent
            spacing: Theme.px(10)
            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: root.label
                color: Theme.text
                font.pixelSize: Theme.px(20)
            }
            Text {
                anchors.verticalCenter: parent.verticalCenter
                visible: text !== ""
                text: root.key
                color: Theme.dim
                font.pixelSize: Theme.px(15)
            }
        }
        MouseArea {
            id: area
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: inputMode.press(root.btn)
        }
    }
}
