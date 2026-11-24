import QtQuick

// The handler's Janus.addOption declarations, then Janus's built-in
// "Display" rows (render scale / upscale filter, keys starting with "@"). ▲▼ picks an option; A toggles
// a switch / cycles a choice / edits text; ◀▶ steps choices and numbers.
// Text is typed on Janus's on-screen keyboard (Keyboard.qml) or a physical one.
Rectangle {
    id: root
    signal closed()
    signal keysReleased()       // text editing ended: keyboard back to the page

    property int current: 0
    property bool editing: false
    property var opts: backend.options     // the games screen's Settings: backend.globalOptions
    property string title: "Options"
    onOptsChanged: current = Math.min(current, Math.max(0, opts.length - 1))
    readonly property var cur: opts.length > 0 ? opts[Math.min(current, opts.length - 1)] : null

    color: "#d8000000"
    visible: false

    // A click outside the box closes it (or cancels typing).
    MouseArea {
        anchors.fill: parent
        onClicked: root.handle("b", -1)
    }

    function open() {
        current = 0
        editing = false
        visible = true
    }

    function close() {
        if (editing) stopEditing(false)
        visible = false
        closed()
    }

    function startEditing() {
        editing = true
        editor.text = cur.text
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
        if (commit) backend.setOptionText(cur.key, editor.text)
        editing = false
        keysReleased()
    }

    function handle(btn, js) {
        if (editing) {
            if (btn === "start") stopEditing(true)
            else if (btn === "b") stopEditing(false)
            else keyboard.handle(btn)
            return
        }
        if (!cur) { if (btn === "b" || btn === "y") close(); return }
        switch (btn) {
        case "up": current = Math.max(0, current - 1); break
        case "down": current = Math.min(opts.length - 1, current + 1); break
        // ◀▶ change a value; an action (a button, type "action") only runs on A.
        // "info" and "link" rows (About) change nothing; a link opens on a click.
        case "left": if (cur.type !== "string" && cur.type !== "action") backend.stepOption(cur.key, -1); break
        case "right": if (cur.type !== "string" && cur.type !== "action") backend.stepOption(cur.key, 1); break
        case "a":
            if (cur.type === "string") startEditing()
            else backend.stepOption(cur.key, 1)
            break
        case "b": case "y": close(); break
        }
    }

    Rectangle {
        anchors.centerIn: parent
        width: Math.min(parent.width - Theme.px(120), Theme.px(1100))
        height: Math.min(parent.height - Theme.px(120), col.implicitHeight + Theme.px(64))
        radius: Theme.px(16)
        color: Theme.panel
        border.color: Theme.line
        border.width: Theme.px(2)
        clip: true
        MouseArea { anchors.fill: parent }    // clicks in the box stay in it

        Column {
            id: col
            anchors { left: parent.left; right: parent.right; top: parent.top; margins: Theme.px(32) }
            spacing: Theme.px(10)

            Text {
                text: root.title
                color: Theme.text
                font.pixelSize: Theme.px(32)
                font.bold: true
                bottomPadding: Theme.px(8)
            }

            Repeater {
                model: root.opts
                delegate: Column {
                    id: entry
                    required property var modelData
                    required property int index
                    // While editing, only the option being edited stays.
                    visible: !root.editing || index === root.current
                    width: col.width

                    // Section header where the group changes (handler options
                    // first, then Janus's built-in "Display" rows).
                    Text {
                        visible: !root.editing && (entry.index === 0
                                 || root.opts[entry.index - 1].group !== entry.modelData.group)
                        text: entry.modelData.group      // Game, Maintenance (actions), Display
                        color: Theme.dim
                        font.pixelSize: Theme.px(18)
                        font.bold: true
                        topPadding: entry.index === 0 ? 0 : Theme.px(14)
                        bottomPadding: Theme.px(4)
                    }

                Rectangle {
                    id: row
                    readonly property var modelData: entry.modelData
                    readonly property int index: entry.index
                    readonly property bool sel: index === root.current
                    width: col.width
                    height: Theme.px(64)
                    radius: Theme.px(10)
                    color: sel ? Theme.panel2 : "transparent"
                    border.color: sel ? Theme.accent : "transparent"
                    border.width: Theme.px(2)

                    // Click: like A (toggle / next choice / edit text);
                    // the ◀ ▶ arrows step back and forth.
                    MouseArea {
                        anchors.fill: parent
                        enabled: !root.editing
                        hoverEnabled: true
                        cursorShape: row.modelData.type === "info" ? Qt.ArrowCursor : Qt.PointingHandCursor
                        onPositionChanged: if (inputMode.mouse) root.current = row.index
                        onClicked: {
                            root.current = row.index
                            // A link (About) opens only on a click, never on A
                            if (row.modelData.type === "link") Qt.openUrlExternally(row.modelData.url)
                            else root.handle("a", -1)
                        }
                    }

                    Text {
                        anchors { left: parent.left; leftMargin: Theme.px(20); verticalCenter: parent.verticalCenter }
                        width: parent.width * 0.5
                        elide: Text.ElideRight
                        text: row.modelData.label
                        color: Theme.text
                        font.pixelSize: Theme.px(22)
                    }

                    // Value: switch, ◀ choice ▶, or text.
                    Row {
                        visible: !(row.sel && root.editing)
                        anchors { right: parent.right; rightMargin: Theme.px(20); verticalCenter: parent.verticalCenter }
                        spacing: Theme.px(14)
                        Text {
                            visible: row.modelData.type === "choice" || row.modelData.type === "int"
                            text: "◀"; color: row.sel ? Theme.text : Theme.line
                            font.pixelSize: Theme.px(20)
                            MouseArea {
                                anchors { fill: parent; margins: -Theme.px(10) }
                                cursorShape: Qt.PointingHandCursor
                                onClicked: {
                                    root.current = row.index
                                    backend.stepOption(row.modelData.key, -1)
                                }
                            }
                        }
                        Rectangle {
                            visible: row.modelData.type === "bool"
                            readonly property bool on: row.modelData.display === "On"
                            anchors.verticalCenter: parent.verticalCenter
                            width: Theme.px(56); height: Theme.px(30); radius: height / 2
                            color: on ? Theme.accent : Theme.line
                            Rectangle {
                                width: parent.height - Theme.px(6); height: width; radius: width / 2
                                anchors.verticalCenter: parent.verticalCenter
                                x: parent.on ? parent.width - width - Theme.px(3) : Theme.px(3)
                                color: "white"
                            }
                        }
                        Text {
                            visible: row.modelData.type !== "bool"
                            width: Math.min(implicitWidth, col.width * 0.4)
                            elide: Text.ElideRight
                            text: row.modelData.display
                            color: row.modelData.type === "link" ? Theme.accent
                                 : row.modelData.display === "(empty)" ? Theme.dim : Theme.text
                            font.pixelSize: Theme.px(22)
                            font.bold: row.modelData.type !== "info"
                            font.underline: row.modelData.type === "link"
                        }
                        Text {
                            visible: row.modelData.type === "choice" || row.modelData.type === "int"
                            text: "▶"; color: row.sel ? Theme.text : Theme.line
                            font.pixelSize: Theme.px(20)
                            MouseArea {
                                anchors { fill: parent; margins: -Theme.px(10) }
                                cursorShape: Qt.PointingHandCursor
                                onClicked: {
                                    root.current = row.index
                                    backend.stepOption(row.modelData.key, 1)
                                }
                            }
                        }
                    }
                }
                }
            }

            // Text editor for the selected string option.
            Rectangle {
                visible: root.editing
                width: col.width
                height: Theme.px(56)
                radius: Theme.px(8)
                color: Theme.bg
                border.color: Theme.accent
                border.width: Theme.px(2)
                TextInput {
                    id: editor
                    anchors { fill: parent; margins: Theme.px(14) }
                    color: Theme.text
                    font.pixelSize: Theme.px(22)
                    clip: true
                    verticalAlignment: TextInput.AlignVCenter
                    Keys.onReturnPressed: root.stopEditing(true)
                    Keys.onEnterPressed: root.stopEditing(true)
                    Keys.onEscapePressed: root.stopEditing(false)
                }
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
                topPadding: Theme.px(10)
                wrapMode: Text.Wrap
                visible: !root.editing
                text: root.cur ? root.cur.description : "This game has no options."
                color: Theme.dim
                font.pixelSize: Theme.px(19)
            }

            Row {
                topPadding: Theme.px(14)
                spacing: Theme.px(36)
                ButtonHint { btn: "updown"; label: "Choose"; visible: !root.editing && !inputMode.mouse }
                ButtonHint { btn: "a"; label: root.editing ? "Type" : "Change"; visible: !!root.cur && root.cur.type !== "info" && root.cur.type !== "link" && !inputMode.mouse }
                ButtonHint { btn: "leftright"; label: "Step"; visible: !root.editing && !!root.cur && ["string", "bool", "action", "info", "link"].indexOf(root.cur.type) < 0 && !inputMode.mouse }
                ButtonHint { btn: "x"; label: "Delete"; visible: root.editing && !inputMode.mouse }
                ButtonHint { btn: "y"; label: "Shift"; visible: root.editing && !inputMode.mouse }
                ButtonHint { btn: "lbrb"; label: "Cursor"; visible: root.editing && !inputMode.mouse }
                ButtonHint { btn: "start"; label: "Save"; visible: root.editing; key: "Enter" }
                ButtonHint { btn: "b"; label: root.editing ? "Cancel" : "Close" }
            }
        }
    }
}
