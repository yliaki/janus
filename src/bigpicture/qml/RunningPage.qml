import QtQuick

// Shown while a session runs, behind the game tiles.
Item {
    id: root

    // Pads do nothing here: players mashing buttons at a game's intro would
    // otherwise stop everything. Stop all and removing a player are
    // mouse-only (or Alt+2). A free pad joins by holding Start (backend).
    function handle(btn, js) {}

    Image {
        anchors.fill: parent
        source: backend.heroBlurUrl
        fillMode: Image.PreserveAspectCrop
        asynchronous: true
        sourceSize.width: 1920
        opacity: 0.3
    }

    Column {
        anchors.centerIn: parent
        width: Math.min(parent.width - Theme.px(112), Theme.px(1100))
        spacing: Theme.px(24)

        Item {
            width: parent.width
            height: logo.visible ? Theme.px(110) : name.implicitHeight
            Image {
                id: logo
                visible: backend.logoUrl !== "" && status === Image.Ready
                anchors { left: parent.left; top: parent.top; bottom: parent.bottom }
                width: Math.min(sourceSize.width * height / Math.max(1, sourceSize.height), parent.width * 0.6)
                source: backend.logoUrl
                fillMode: Image.PreserveAspectFit
                horizontalAlignment: Image.AlignLeft
                asynchronous: true
            }
            Text {
                id: name
                visible: !logo.visible
                width: parent.width
                text: backend.gameName
                color: Theme.text
                font.pixelSize: Theme.px(44)
                font.bold: true
                elide: Text.ElideRight
            }
        }

        Text {
            text: backend.launching ? "Starting the games…" : "Playing"
            color: Theme.dim
            font.pixelSize: Theme.px(24)
        }

        Repeater {
            model: backend.sessionRows
            delegate: Rectangle {
                id: row
                required property var modelData
                required property int index
                readonly property color pcolor: Theme.players[(modelData.number - 1) % Theme.players.length]
                readonly property bool failed: modelData.status.startsWith("Error")
                readonly property bool gone: failed || modelData.status.startsWith("Exited")
                visible: modelData.number > 0
                width: parent.width
                height: rowCol.implicitHeight + Theme.px(32)
                radius: Theme.px(10)
                color: Theme.panel
                Rectangle {
                    width: Theme.px(8); height: parent.height; radius: Theme.px(4)
                    color: row.pcolor
                }
                Column {
                    id: rowCol
                    anchors { left: parent.left; right: parent.right; verticalCenter: parent.verticalCenter
                              leftMargin: Theme.px(32); rightMargin: Theme.px(28) }
                    spacing: Theme.px(6)
                    Item {
                        width: parent.width
                        height: Theme.px(30)
                        Text {
                            anchors.verticalCenter: parent.verticalCenter
                            text: "P" + row.modelData.number + "   " + row.modelData.name
                            color: Theme.text
                            font.pixelSize: Theme.px(22)
                            font.bold: true
                        }
                        // Take this player out (their game closes, the others re-flow).
                        Rectangle {
                            id: removeBtn
                            visible: !row.gone
                            anchors { right: parent.right; verticalCenter: parent.verticalCenter }
                            width: removeText.implicitWidth + Theme.px(24)
                            height: Theme.px(34)
                            radius: Theme.px(8)
                            color: removeArea.containsMouse ? Theme.danger : "transparent"
                            border.color: Theme.danger
                            border.width: Theme.px(2)
                            Text {
                                id: removeText
                                anchors.centerIn: parent
                                text: "Remove"
                                color: removeArea.containsMouse ? "white" : Theme.danger
                                font.pixelSize: Theme.px(18)
                            }
                            MouseArea {
                                id: removeArea
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: backend.removeInstance(row.index)
                            }
                        }
                        Text {
                            visible: !row.failed
                            anchors { right: removeBtn.visible ? removeBtn.left : parent.right
                                      rightMargin: removeBtn.visible ? Theme.px(20) : 0
                                      verticalCenter: parent.verticalCenter }
                            text: row.modelData.status
                            color: row.modelData.status.startsWith("Running") ? Theme.text : Theme.dim
                            font.pixelSize: Theme.px(20)
                            // Pulse while this instance is still on its way.
                            SequentialAnimation on opacity {
                                running: !row.modelData.status.startsWith("Running")
                                         && !row.modelData.status.startsWith("Exited")
                                loops: Animation.Infinite
                                alwaysRunToEnd: true
                                NumberAnimation { to: 0.35; duration: 600; easing.type: Easing.InOutQuad }
                                NumberAnimation { to: 1.0; duration: 600; easing.type: Easing.InOutQuad }
                            }
                        }
                    }
                    // Errors get their own wrapped line instead of being cut off.
                    Text {
                        visible: row.failed
                        width: parent.width
                        wrapMode: Text.Wrap
                        text: row.modelData.status
                        color: Theme.danger
                        font.pixelSize: Theme.px(19)
                    }
                    Text {
                        visible: row.modelData.lobby !== ""
                        text: "Goldberg lobby  " + row.modelData.lobby
                        color: Theme.accent
                        font.pixelSize: Theme.px(19)
                        font.family: "monospace"
                    }
                }
            }
        }

        Rectangle {
            width: Theme.px(320)
            height: Theme.px(72)
            radius: Theme.px(12)
            color: Theme.danger
            opacity: stopArea.containsMouse ? 0.85 : 1
            MouseArea {
                id: stopArea
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: backend.stop()
            }
            Text {
                anchors.centerIn: parent
                text: "Stop all"
                color: "white"
                font.pixelSize: Theme.px(26)
                font.bold: true
            }
        }

        Text {
            visible: backend.joinHint !== ""
            text: backend.joinHint
            color: Theme.accent
            font.pixelSize: Theme.px(22)
        }

        Text {
            text: "Alt+2 stops all games from anywhere · Alt+1 releases the mouse from a game"
            color: Theme.dim
            font.pixelSize: Theme.px(20)
            wrapMode: Text.Wrap
            width: parent.width
        }
    }
}
