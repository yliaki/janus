import QtQuick

// "Who's playing?" for one pad (X on the players screen). ▲▼ picks a
// profile, A takes it (one another player has: the two swap), Y renames it, Back deletes it (after typing "delete"),
// "New player…" types a name on the on-screen keyboard. Only the pad that
// opened it (or the keyboard) drives it. A profile is a person: their name in
// the game and their own saves; each row says what they have saved for this
// game (size + last played, or the handler's Game.profileInfo).
Rectangle {
    id: root
    signal keysReleased()       // text editing ended: keyboard back to the page

    property int js: -1
    property int number: 0      // the player's number, for the title/color
    property int current: 0
    property string editMode: ""     // "", "new", "rename", "delete"
    property string error: ""
    readonly property var rows: backend.profiles.concat([{ id: "", name: "New player…", owner: false, takenBy: 0 }])
    readonly property var cur: rows[Math.min(current, rows.length - 1)]
    readonly property bool editing: editMode !== ""
    readonly property bool canDelete: cur.id !== "" && !cur.owner
                                      && !(cur.takenBy > 0 && cur.takenBy !== number)
    readonly property color pcolor: number > 0 ? Theme.players[(number - 1) % Theme.players.length] : Theme.accent

    color: "#d8000000"
    visible: false

    // A click outside the box closes it (or cancels typing).
    MouseArea {
        anchors.fill: parent
        onClicked: root.handle("b", -1)
    }

    function open(padJs, playerNumber) {
        js = padJs
        number = playerNumber
        editMode = ""
        error = ""
        const mine = backend.profileOf(padJs)
        current = Math.max(0, rows.findIndex(r => r.id === mine))
        backend.refreshProfileInfo()
        visible = true
    }

    function close() {
        if (editing) stopEditing(false)
        visible = false
    }

    function startEditing(mode) {
        editMode = mode
        error = ""
        editor.text = mode === "rename" ? cur.name : ""
        editor.cursorPosition = editor.text.length
        keyboard.reset()
        editor.forceActiveFocus()
    }

    function typeText(t) {
        if (editor.selectedText.length > 0) editor.remove(editor.selectionStart, editor.selectionEnd)
        editor.insert(editor.cursorPosition, t)
    }

    function deleteBack() {
        if (editor.selectedText.length > 0) editor.remove(editor.selectionStart, editor.selectionEnd)
        else if (editor.cursorPosition > 0) editor.remove(editor.cursorPosition - 1, editor.cursorPosition)
    }

    function stopEditing(commit) {
        if (!editing) return
        if (commit) {
            const err = editMode === "new" ? backend.createProfile(js, editor.text)
                      : editMode === "delete" ? backend.deleteProfile(js, cur.id, editor.text)
                      : backend.renameProfile(cur.id, editor.text)
            if (err !== "") { error = err; return }
        }
        const created = commit && editMode === "new"
        if (commit && editMode === "delete")
            current = Math.min(current, rows.length - 1)
        editMode = ""
        keysReleased()
        if (created) visible = false
    }

    function choose() {
        if (cur.id === "") { startEditing("new"); return }
        // Taken by another player: the backend swaps the two.
        backend.setProfile(js, cur.id)
        visible = false
    }

    function handle(btn, padJs) {
        if (padJs >= 0 && padJs !== js) return
        if (editing) {
            if (btn === "start") stopEditing(true)
            else if (btn === "b") stopEditing(false)
            else keyboard.handle(btn)
            return
        }
        switch (btn) {
        case "up": current = Math.max(0, current - 1); break
        case "down": current = Math.min(rows.length - 1, current + 1); break
        case "a": choose(); break
        case "y": if (cur.id !== "" && !cur.owner) startEditing("rename"); break
        case "back": if (canDelete) startEditing("delete"); break
        case "b": case "x": close(); break
        }
    }

    Rectangle {
        anchors.centerIn: parent
        width: Math.min(parent.width - Theme.px(120), Theme.px(900))
        height: Math.min(parent.height - Theme.px(120), col.implicitHeight + Theme.px(64))
        radius: Theme.px(16)
        color: Theme.panel
        border.color: root.pcolor
        border.width: Theme.px(2)
        clip: true
        MouseArea { anchors.fill: parent }    // clicks in the box stay in it

        Column {
            id: col
            anchors { left: parent.left; right: parent.right; top: parent.top; margins: Theme.px(32) }
            spacing: Theme.px(10)

            Text {
                text: root.editMode === "new" ? "New player"
                      : root.editMode === "rename" ? "Rename " + root.cur.name
                      : root.editMode === "delete" ? "Delete " + root.cur.name + "?"
                      : "P" + root.number + " — who's playing?"
                color: root.pcolor
                font.pixelSize: Theme.px(32)
                font.bold: true
                bottomPadding: Theme.px(8)
            }

            Repeater {
                model: root.rows
                delegate: Rectangle {
                    id: row
                    required property var modelData
                    required property int index
                    readonly property bool sel: index === root.current
                    readonly property bool other: modelData.takenBy > 0 && modelData.takenBy !== root.number
                    visible: !root.editing || (sel && (root.editMode === "rename" || root.editMode === "delete"))
                    readonly property string info: modelData.info || ""
                    width: col.width
                    height: Theme.px(info !== "" ? 76 : 60)
                    radius: Theme.px(10)
                    color: sel ? Theme.panel2 : "transparent"
                    border.color: sel ? root.pcolor : "transparent"
                    border.width: Theme.px(2)
                    opacity: other && !sel ? 0.7 : 1

                    MouseArea {
                        anchors.fill: parent
                        enabled: !root.editing
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onPositionChanged: if (inputMode.mouse) root.current = row.index
                        onClicked: {
                            root.current = row.index
                            root.choose()
                        }
                    }

                    Column {
                        anchors { left: parent.left; leftMargin: Theme.px(20); verticalCenter: parent.verticalCenter }
                        width: parent.width * 0.7
                        Text {
                            width: parent.width
                            elide: Text.ElideRight
                            text: row.modelData.name
                            color: row.modelData.id === "" ? Theme.dim : Theme.text
                            font.pixelSize: Theme.px(24)
                            font.bold: row.modelData.takenBy === root.number && root.number > 0
                        }
                        // What this profile has saved for the game.
                        Text {
                            visible: row.info !== ""
                            width: parent.width
                            elide: Text.ElideRight
                            text: row.info
                            color: Theme.dim
                            font.pixelSize: Theme.px(17)
                        }
                    }
                    Text {
                        anchors { right: parent.right; rightMargin: Theme.px(20); verticalCenter: parent.verticalCenter }
                        text: row.other ? "P" + row.modelData.takenBy + (row.sel ? " · swap" : "")
                              : row.modelData.takenBy > 0 ? "P" + row.modelData.takenBy
                              : row.modelData.owner ? backend.ownerLabel : ""
                        color: row.modelData.takenBy > 0
                               ? Theme.players[(row.modelData.takenBy - 1) % Theme.players.length]
                               : Theme.dim
                        font.pixelSize: Theme.px(19)
                        font.bold: row.modelData.takenBy > 0
                    }
                }
            }

            Text {
                visible: root.editMode === "delete"
                width: col.width
                wrapMode: Text.Wrap
                text: root.editMode === "delete" ? backend.profileDeleteText(root.cur.id)
                      + "\nType delete to confirm." : ""
                color: Theme.danger
                font.pixelSize: Theme.px(19)
            }

            Rectangle {
                visible: root.editing
                width: col.width
                height: Theme.px(56)
                radius: Theme.px(8)
                color: Theme.bg
                border.color: root.pcolor
                border.width: Theme.px(2)
                TextInput {
                    id: editor
                    anchors { fill: parent; margins: Theme.px(14) }
                    color: Theme.text
                    font.pixelSize: Theme.px(22)
                    clip: true
                    maximumLength: 32
                    verticalAlignment: TextInput.AlignVCenter
                    Text {
                        visible: editor.text === "" && root.editMode === "delete"
                        anchors.verticalCenter: parent.verticalCenter
                        text: "delete"
                        color: Theme.dim
                        font.pixelSize: Theme.px(22)
                        opacity: 0.5
                    }
                    Keys.onReturnPressed: root.stopEditing(true)
                    Keys.onEnterPressed: root.stopEditing(true)
                    Keys.onEscapePressed: root.stopEditing(false)
                }
            }

            Text {
                visible: root.error !== ""
                width: col.width
                wrapMode: Text.Wrap
                text: root.error
                color: Theme.danger
                font.pixelSize: Theme.px(19)
            }

            Keyboard {
                id: keyboard
                visible: root.editing && !inputMode.mouse   // else a physical keyboard types
                width: col.width
                height: implicitHeight
                onTyped: (t) => root.typeText(t)
                onBackspace: root.deleteBack()
                onCursorMove: (d) => editor.cursorPosition = Math.max(0, Math.min(editor.text.length, editor.cursorPosition + d))
                onDone: root.stopEditing(true)
            }

            Text {
                width: col.width
                topPadding: Theme.px(6)
                wrapMode: Text.Wrap
                visible: !root.editing
                text: root.cur.id === "" ? "A new player starts with empty saves."
                      : root.cur.owner ? (backend.ownerLabel === "Steam account"
                                          ? "Plays as your Steam account, on your real saves."
                                          : "Plays as your " + backend.ownerLabel + ".")
                      : "Their own name in the game and their own saves."
                color: Theme.dim
                font.pixelSize: Theme.px(19)
            }

            Row {
                topPadding: Theme.px(14)
                spacing: Theme.px(36)
                ButtonHint { btn: "updown"; label: "Choose"; visible: !root.editing && !inputMode.mouse }
                ButtonHint { btn: "a"; label: root.editing ? "Type" : (root.cur.takenBy > 0 && root.cur.takenBy !== root.number ? "Swap with P" + root.cur.takenBy : "Play as"); visible: !inputMode.mouse }
                ButtonHint { btn: "y"; label: root.editing ? "Shift" : "Rename"; visible: (root.editing && !inputMode.mouse) || (!root.editing && root.cur.id !== "" && !root.cur.owner) }
                ButtonHint { btn: "x"; label: "Delete"; visible: root.editing && !inputMode.mouse }
                ButtonHint { btn: "back"; label: "Delete"; key: "Del"; visible: !root.editing && root.canDelete }
                ButtonHint { btn: "start"; label: root.editMode === "delete" ? "Delete" : "Save"; visible: root.editing; key: "Enter" }
                ButtonHint { btn: "b"; label: root.editing ? "Cancel" : "Close" }
            }
        }
    }
}
