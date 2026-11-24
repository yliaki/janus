import QtQuick

// "Set up a controller" (setup_wizard.py): pick a pad, then press what it asks
// for, one control at a time. The pad being set up only answers; other pads,
// the keyboard or the mouse steer: X skips a step, Y goes back, B cancels.
// The pad list shows how each pad is mapped; X removes a Janus mapping.
Rectangle {
    id: root
    color: "#e0000000"
    visible: padSetup.active
    MouseArea { anchors.fill: parent }        // nothing behind takes clicks

    Rectangle {
        anchors.centerIn: parent
        width: Math.min(parent.width - Theme.px(120), Theme.px(1000))
        height: col.implicitHeight + Theme.px(64)
        radius: Theme.px(16)
        color: Theme.panel
        border.color: Theme.accent
        border.width: Theme.px(2)

        Column {
            id: col
            anchors { left: parent.left; right: parent.right; top: parent.top; margins: Theme.px(32) }
            spacing: Theme.px(18)

            Text {
                width: parent.width
                text: padSetup.confirming !== "" ? "Remove mapping"
                : padSetup.choosing ? "Set up a controller"
                    : padSetup.message !== "" ? "Controller setup" : "Set up " + padSetup.padName
                color: Theme.text
                elide: Text.ElideRight
                font.pixelSize: Theme.px(32)
                font.bold: true
            }

            // 0. Removing a Janus mapping: are you sure?
            Text {
                visible: padSetup.confirming !== ""
                width: parent.width
                wrapMode: Text.Wrap
                text: "Remove the button mapping you set up for " + padSetup.confirming
                    + "? It's removed for every game. You can set it up again any time."
                color: Theme.text
                font.pixelSize: Theme.px(24)
            }

            // 1. Which pad
            readonly property bool listing: padSetup.choosing && padSetup.confirming === ""
            Text {
                visible: padSetup.info !== "" && col.listing
                width: parent.width
                wrapMode: Text.Wrap
                text: padSetup.info
                color: Theme.accent
                font.pixelSize: Theme.px(20)
            }
            Text {
                visible: col.listing
                width: parent.width
                wrapMode: Text.Wrap
                text: "Which controller? Pads Janus input doesn't recognise come first."
                color: Theme.dim
                font.pixelSize: Theme.px(20)
            }
            Repeater {
                model: col.listing ? padSetup.choices : []
                delegate: Rectangle {
                    required property var modelData
                    required property int index
                    readonly property bool sel: index === padSetup.current
                    width: col.width
                    height: Theme.px(60)
                    radius: Theme.px(10)
                    color: sel ? Theme.panel2 : "transparent"
                    border.color: sel ? Theme.accent : "transparent"
                    border.width: Theme.px(2)
                    MouseArea {
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onEntered: padSetup.select(parent.index)
                        onClicked: padSetup.choose(parent.index)
                    }
                    Text {
                        anchors { left: parent.left; leftMargin: Theme.px(20); verticalCenter: parent.verticalCenter }
                        text: parent.modelData.name
                        color: Theme.text
                        font.pixelSize: Theme.px(22)
                    }
                    Text {
                        anchors { right: parent.right; rightMargin: Theme.px(20); verticalCenter: parent.verticalCenter }
                        readonly property string st: parent.modelData.status
                        text: st === "janus" ? "set up in Janus"
                            : st === "known" ? "recognised" : "not recognised"
                        color: st === "janus" ? Theme.accent
                            : st === "unknown" ? Theme.warning : Theme.dim
                        font.pixelSize: Theme.px(18)
                    }
                }
            }

            // 2. Recording
            Text {
                visible: !padSetup.choosing && padSetup.message === ""
                text: "Step " + (padSetup.step + 1) + " of " + padSetup.steps
                color: Theme.dim
                font.pixelSize: Theme.px(20)
            }
            Rectangle {
                visible: !padSetup.choosing && padSetup.message === ""
                width: parent.width
                height: Theme.px(8)
                radius: height / 2
                color: Theme.line
                Rectangle {
                    width: parent.width * padSetup.step / Math.max(1, padSetup.steps)
                    height: parent.height
                    radius: height / 2
                    color: Theme.accent
                }
            }
            Text {
                visible: !padSetup.choosing && padSetup.message === ""
                width: parent.width
                wrapMode: Text.Wrap
                text: padSetup.prompt
                color: Theme.text
                font.pixelSize: Theme.px(36)
                font.bold: true
                topPadding: Theme.px(12)
                bottomPadding: Theme.px(12)
            }

            // The pad: what's asked for pulses, recorded controls light up
            // while pressed (and after saving, the whole mapping can be tried).
            XboxPad {
                visible: !padSetup.choosing && (padSetup.message === "" || padSetup.testing)
                anchors.horizontalCenter: parent.horizontalCenter
                width: Math.min(parent.width, Theme.px(520))
                target: padSetup.message === "" ? padSetup.target : ""
                mapped: padSetup.mapped
                live: padSetup.live
            }
            Text {
                visible: padSetup.note !== "" && padSetup.message === ""
                width: parent.width
                wrapMode: Text.Wrap
                text: padSetup.note
                color: Theme.warning
                font.pixelSize: Theme.px(20)
            }

            // 3. Done / failed
            Text {
                visible: padSetup.message !== ""
                width: parent.width
                wrapMode: Text.Wrap
                text: padSetup.message
                color: Theme.text
                font.pixelSize: Theme.px(24)
            }

            // Hints. While recording (and while trying the result out) the pad
            // being set up only answers, so the hints say what steers instead;
            // with no other pad they're clickable buttons (keyboard keys shown).
            readonly property bool recording: !padSetup.choosing && padSetup.message === ""
            readonly property var curChoice: padSetup.choices[padSetup.current]
            Text {
                visible: col.recording
                width: parent.width
                wrapMode: Text.Wrap
                text: padSetup.otherPads
                    ? "The controller being set up only answers. Skip, go back or cancel with another controller, the keyboard or the mouse:"
                    : "The controller being set up only answers. Skip, go back or cancel with the keyboard or the mouse:"
                color: Theme.dim
                font.pixelSize: Theme.px(17)
            }
            Row {
                spacing: Theme.px(34)
                // Picking a pad / confirming a removal: any pad steers.
                ButtonHint { btn: "updown"; label: "Choose"; visible: col.listing && !inputMode.mouse }
                ButtonHint { btn: "a"; label: "Set up"; visible: col.listing }
                ButtonHint { btn: "x"; label: "Remove mapping"
                             visible: col.listing && col.curChoice !== undefined && col.curChoice.status === "janus" }
                ButtonHint { btn: "a"; label: "Remove"; visible: padSetup.confirming !== "" }
                ButtonHint { btn: "b"; label: "Keep"; visible: padSetup.confirming !== "" }
                ButtonHint { btn: "b"; label: "Close"; visible: col.listing }
                // Recording: other pads / keyboard / mouse only.
                ButtonHint { btn: "x"; label: "Skip"; visible: col.recording; forceButton: !padSetup.otherPads }
                ButtonHint { btn: "y"; label: "Back"; visible: col.recording && padSetup.step > 0; forceButton: !padSetup.otherPads }
                ButtonHint { btn: "b"; label: "Cancel"; visible: col.recording; forceButton: !padSetup.otherPads }
                // Trying it out: the tested pad's own Start works (A would be
                // a test press); any other pad's Start or A too.
                ButtonHint { btn: "start"; label: "Finish"; visible: padSetup.testing }
                // Done / failed, nothing being tested: every pad steers again.
                ButtonHint { btn: "a"; label: "OK"; visible: padSetup.message !== "" && !padSetup.testing }
            }
        }
    }
}
