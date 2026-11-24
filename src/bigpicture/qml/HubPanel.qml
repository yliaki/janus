import QtQuick

// "Get handlers" from the handler hub (Y on the games screen). ▲▼ picks a
// row, A on "Search…" types a name on the on-screen keyboard (the list
// follows as you type), A on "All my games" fetches every missing/outdated
// handler for the games in your Steam library (installed or not), "Update"
// every newer version of an installed handler, A on a game fetches its
// handler. LT/RT (or A on the "Sort" row) changes the order, most installed
// first by default; the "Show" row hides games outside your library. Every
// install is confirmed first, showing the version, its uploader and the hub
// page where the source can be read.
Rectangle {
    id: root
    signal keysReleased()       // text editing ended: keyboard back to the page

    readonly property var hub: backend.hub
    readonly property var rows: [{ kind: "search" }, { kind: "all" }]
                                .concat(hub.updateCount > 0 ? [{ kind: "updates" }] : [])
                                .concat([{ kind: "sort" }, { kind: "mine" }])
                                .concat(hub.rows.map(r => Object.assign({ kind: "game" }, r)))
    property string queryBefore: ""     // restored when a search is cancelled
    property bool editing: false
    property int cur: 0         // selected row; kept when the list reloads

    color: "#d8000000"
    visible: false

    // A click outside the box closes it (or ends a search / cancels a confirm).
    MouseArea {
        anchors.fill: parent
        onClicked: root.handle("b", -1)
    }

    function open() {
        editing = false
        cur = 0
        visible = true
        hub.reload()
    }

    // Straight to one game's handler (a game picked that has none).
    function openFor(appid, name) {
        editing = false
        cur = 0
        visible = true
        hub.askFor(appid, name)
    }

    function close() {
        if (editing) stopEditing(false)
        hub.cancel()
        visible = false
    }

    function startEditing() {
        editing = true
        queryBefore = hub.query
        editor.text = hub.query
        editor.cursorPosition = editor.text.length
        keyboard.reset()
        editor.forceActiveFocus()
    }

    function stopEditing(commit) {
        if (!editing) return
        editing = false
        keysReleased()
        liveSearch.stop()
        hub.search(commit ? editor.text : queryBefore)
        if (commit) cur = 0
    }

    // The list follows the search box as you type.
    Timer {
        id: liveSearch
        interval: 350
        onTriggered: if (root.editing) root.hub.search(editor.text)
    }

    function typeText(t) {
        if (editor.selectedText.length > 0) editor.remove(editor.selectionStart, editor.selectionEnd)
        editor.insert(editor.cursorPosition, t)
    }

    function deleteBack() {
        if (editor.selectedText.length > 0) editor.remove(editor.selectionStart, editor.selectionEnd)
        else if (editor.cursorPosition > 0) editor.remove(editor.cursorPosition - 1, editor.cursorPosition)
    }

    function choose() {
        const r = rows[cur]
        if (!r) return
        if (r.kind === "search") startEditing()
        else if (r.kind === "all") hub.askAll()
        else if (r.kind === "updates") hub.askUpdates()
        else if (r.kind === "sort") hub.cycleSort(1)
        else if (r.kind === "mine") hub.toggleMine()
        else hub.ask(r.id)
    }

    function rowText(r) {
        switch (r.kind) {
        case "search": return hub.query !== "" ? "Search: " + hub.query : "Search…"
        case "all": return "All my games" + (hub.missingCount > 0 ? " (" + hub.missingCount + " to get)" : "")
        case "updates": return "Update " + hub.updateCount + " handler" + (hub.updateCount === 1 ? "" : "s")
        case "sort": return "Sort: " + hub.sortLabel
        case "mine": return "Show: " + (hub.mine ? "My library" : "All games")
        }
        return r.name
    }

    function statusText(r) {
        if (r.status === "current") return "Handler v" + r.version
        if (r.status === "update") return "Update v" + r.version + " → v" + r.latest
        if (r.handler) return "Has a handler"
        return "Get v" + r.latest
    }

    function whereText(r) {
        switch (r.where) {
        case "library": return "Not installed"
        case "store": return "Not in your library"
        case "janus": return "Not on Steam"
        }
        return ""
    }

    function installsText(n) {
        return (n >= 10000 ? Math.round(n / 1000) + "k" : n >= 1000 ? (n / 1000).toFixed(1) + "k" : n)
               + " ⤓"
    }

    function handle(btn, js) {
        if (editing) {
            if (btn === "start") stopEditing(true)
            else if (btn === "b") stopEditing(false)
            else keyboard.handle(btn)
            return
        }
        if (hub.confirm) {
            if (btn === "a" && hub.busy === "") hub.accept()
            else if (btn === "b") hub.back()
            else if (btn === "x") hub.askRemove()          // a handler from the hub
            else if (btn === "down" || btn === "up") {      // scroll long notes
                const step = (btn === "down" ? 1 : -1) * Theme.px(120)
                confirmView.contentY = Math.max(0, Math.min(confirmView.contentHeight - confirmView.height,
                                                            confirmView.contentY + step))
            }
            return
        }
        switch (btn) {
        case "up": cur = Math.max(0, cur - 1); break
        case "down": cur = Math.min(rows.length - 1, cur + 1); break
        case "a": if (hub.busy === "") choose(); break
        case "x": startEditing(); break
        case "lt": hub.cycleSort(-1); break
        case "rt": hub.cycleSort(1); break
        case "b": case "y": close(); break
        }
    }

    Rectangle {
        id: box
        anchors.centerIn: parent
        width: Math.min(parent.width - Theme.px(120), Theme.px(1100))
        height: parent.height - Theme.px(140)
        radius: Theme.px(16)
        color: Theme.panel
        border.color: Theme.accent
        border.width: Theme.px(2)
        clip: true
        MouseArea { anchors.fill: parent }    // clicks in the box stay in it

        Text {
            id: title
            anchors { left: parent.left; top: parent.top; margins: Theme.px(32) }
            text: "Get handlers"
            color: Theme.text
            font.pixelSize: Theme.px(32)
            font.bold: true
        }
        Text {
            anchors { left: title.right; baseline: title.baseline; leftMargin: Theme.px(20) }
            text: "from the Janus handler hub"
            color: Theme.dim
            font.pixelSize: Theme.px(20)
        }

        ListView {
            id: list
            // While typing, the results follow below the keyboard.
            visible: !root.hub.confirm
            anchors {
                left: parent.left; right: parent.right; bottom: statusLine.top
                top: root.editing ? editColumn.bottom : title.bottom
                leftMargin: Theme.px(32); rightMargin: Theme.px(32)
                topMargin: Theme.px(20); bottomMargin: Theme.px(10)
            }
            clip: true
            spacing: Theme.px(6)
            model: root.editing ? root.rows.filter(r => r.kind === "game") : root.rows
            currentIndex: root.editing ? -1 : Math.min(root.cur, root.rows.length - 1)
            keyNavigationEnabled: false
            highlightRangeMode: ListView.ApplyRange
            preferredHighlightBegin: Theme.px(80)
            preferredHighlightEnd: height - Theme.px(80)
            highlightMoveDuration: 120

            delegate: Rectangle {
                id: row
                required property var modelData
                required property int index
                readonly property bool sel: ListView.isCurrentItem && !root.editing
                width: list.width
                height: Theme.px(60)
                radius: Theme.px(10)
                color: sel ? Theme.panel2 : "transparent"
                border.color: sel ? Theme.accent : "transparent"
                border.width: Theme.px(2)

                MouseArea {
                    anchors.fill: parent
                    enabled: !root.editing       // typing: the list only previews
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onPositionChanged: if (inputMode.mouse) root.cur = row.index
                    onClicked: {
                        root.cur = row.index
                        if (root.hub.busy === "") root.choose()
                    }
                }

                Text {
                    anchors { left: parent.left; leftMargin: Theme.px(20); verticalCenter: parent.verticalCenter }
                    width: parent.width * 0.55
                    elide: Text.ElideRight
                    text: root.rowText(row.modelData)
                    color: row.modelData.kind === "game" ? Theme.text : Theme.accent
                    font.pixelSize: Theme.px(24)
                    font.bold: row.modelData.kind !== "game"
                }
                Row {
                    visible: row.modelData.kind === "game"
                    anchors { right: parent.right; rightMargin: Theme.px(20); verticalCenter: parent.verticalCenter }
                    spacing: Theme.px(24)
                    Text {
                        text: row.modelData.kind === "game" ? root.whereText(row.modelData) : ""
                        color: Theme.dim
                        font.pixelSize: Theme.px(19)
                    }
                    Text {
                        text: row.modelData.kind === "game" ? root.installsText(row.modelData.installs) : ""
                        color: Theme.dim
                        font.pixelSize: Theme.px(19)
                        width: Theme.px(80)
                        horizontalAlignment: Text.AlignRight
                    }
                    Text {
                        text: row.modelData.kind === "game" ? root.statusText(row.modelData) : ""
                        color: row.modelData.status === "update" ? Theme.warning
                               : row.modelData.status === "current" || row.modelData.handler ? Theme.dim : Theme.text
                        font.pixelSize: Theme.px(19)
                        font.bold: row.modelData.status !== "current" && !row.modelData.handler
                        width: Theme.px(200)
                        horizontalAlignment: Text.AlignRight
                    }
                }
            }
        }

        Text {
            anchors.centerIn: list
            visible: list.visible && !root.editing && root.hub.busy === "" && root.hub.rows.length === 0
                     && root.hub.error === ""
            text: root.hub.query !== "" ? "No handler matches \"" + root.hub.query + "\"."
                  : root.hub.mine ? "The hub has no handlers for games in your library yet."
                  : "The hub has no handlers yet."
            color: Theme.dim
            font.pixelSize: Theme.px(22)
        }

        // Typing a search.
        Column {
            id: editColumn
            visible: root.editing
            anchors { left: parent.left; right: parent.right; top: title.bottom; margins: Theme.px(32) }
            spacing: Theme.px(16)
            Rectangle {
                width: parent.width
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
                    maximumLength: 64
                    verticalAlignment: TextInput.AlignVCenter
                    Keys.onReturnPressed: root.stopEditing(true)
                    Keys.onEnterPressed: root.stopEditing(true)
                    Keys.onEscapePressed: root.stopEditing(false)
                    onTextChanged: if (root.editing) liveSearch.restart()
                }
            }
            Keyboard {
                id: keyboard
                visible: !inputMode.mouse      // a physical keyboard types into the box
                width: parent.width
                height: implicitHeight
                onTyped: (t) => root.typeText(t)
                onBackspace: root.deleteBack()
                onCursorMove: (d) => editor.cursorPosition = Math.max(0, Math.min(editor.text.length, editor.cursorPosition + d))
                onDone: root.stopEditing(true)
            }
        }

        // Confirming an install: one game in detail (cover, QR code of its hub
        // page, notes, recent changes, contributors), several as a list.
        Flickable {
            id: confirmView
            visible: !!root.hub.confirm
            anchors {
                left: parent.left; right: parent.right; top: title.bottom; bottom: statusLine.top
                leftMargin: Theme.px(32); rightMargin: Theme.px(32); topMargin: Theme.px(20)
            }
            clip: true
            contentHeight: confirmCol.implicitHeight
            readonly property var one: root.hub.confirm && !root.hub.confirm.all
                                       ? root.hub.confirm.items[0] : null
            readonly property bool removing: !!root.hub.confirm && !!root.hub.confirm.remove
            onOneChanged: contentY = 0
            Column {
                id: confirmCol
                width: confirmView.width
                spacing: Theme.px(14)
                Text {
                    text: !root.hub.confirm ? ""
                          : root.hub.confirm.all ? "Install " + root.hub.confirm.items.length + " handlers?"
                          : confirmView.removing ? "Remove the " + confirmView.one.name + " handler?"
                          : confirmView.one.have === 0 ? "Install the " + confirmView.one.name + " handler?"
                          : confirmView.one.have < confirmView.one.version
                          ? "Update the " + confirmView.one.name + " handler to version " + confirmView.one.version + "?"
                          : "Reinstall the " + confirmView.one.name + " handler?"
                    width: parent.width
                    wrapMode: Text.Wrap
                    color: Theme.text
                    font.pixelSize: Theme.px(26)
                    font.bold: true
                }

                // Removing one game's handler.
                Text {
                    visible: confirmView.removing
                    width: parent.width
                    wrapMode: Text.Wrap
                    text: !confirmView.one ? ""
                          : "Janus can't play " + confirmView.one.name + " without a handler, so it "
                            + "leaves the games screen (or shows as \"Get handler\"). You can get the "
                            + "handler again from this list at any time. The game's settings and the "
                            + "players' saves stay."
                    color: Theme.text
                    font.pixelSize: Theme.px(21)
                }

                // One game.
                Row {
                    visible: confirmView.one !== null && !confirmView.removing
                    width: parent.width
                    spacing: Theme.px(28)
                    Column {
                        id: side
                        width: Math.min(Theme.px(300), parent.width * 0.34)
                        spacing: Theme.px(12)
                        Image {
                            width: parent.width
                            height: status === Image.Ready ? width * implicitHeight / Math.max(1, implicitWidth) : 0
                            source: confirmView.one ? confirmView.one.cover : ""
                            fillMode: Image.PreserveAspectFit
                            asynchronous: true
                        }
                        Rectangle {
                            visible: qr.status === Image.Ready
                            width: Theme.px(176); height: width
                            color: "white"
                            radius: Theme.px(8)
                            Image {
                                id: qr
                                anchors.fill: parent
                                anchors.margins: Theme.px(6)
                                source: confirmView.one ? confirmView.one.qr : ""
                                smooth: false
                                asynchronous: true
                            }
                        }
                        Text {
                            width: parent.width
                            wrapMode: Text.Wrap
                            text: (qr.status === Image.Ready ? "Scan to read the handler's source on your phone:\n" : "Its source: ")
                                  + (confirmView.one ? confirmView.one.url : "")
                            color: Theme.dim
                            font.pixelSize: Theme.px(16)
                        }
                    }
                    Column {
                        width: parent.width - side.width - parent.spacing
                        spacing: Theme.px(10)
                        Text {
                            width: parent.width
                            wrapMode: Text.Wrap
                            text: !confirmView.one ? ""
                                  : "Version " + confirmView.one.version + " by " + confirmView.one.uploader
                                    + (confirmView.one.contributors.length > 1
                                       ? "  ·  contributors: " + confirmView.one.contributors.join(", ") : "")
                            color: Theme.text
                            font.pixelSize: Theme.px(21)
                        }
                        Text {
                            width: parent.width
                            wrapMode: Text.Wrap
                            visible: !!confirmView.one && confirmView.one.needs !== ""
                            text: !confirmView.one ? ""
                                  : "Version " + confirmView.one.latest + " needs Janus "
                                    + confirmView.one.needs + " or newer, so this is the newest "
                                    + "version this Janus can run."
                            color: Theme.warning
                            font.pixelSize: Theme.px(18)
                        }
                        Text {
                            width: parent.width
                            wrapMode: Text.Wrap
                            visible: text !== ""
                            text: confirmView.one ? confirmView.one.description : ""
                            color: Theme.text
                            font.pixelSize: Theme.px(19)
                            maximumLineCount: 10
                            elide: Text.ElideRight
                        }
                        Text {
                            text: "Changes"
                            color: Theme.dim
                            font.pixelSize: Theme.px(17)
                            font.bold: true
                            topPadding: Theme.px(6)
                        }
                        Repeater {
                            model: confirmView.one ? confirmView.one.history : []
                            delegate: Text {
                                required property var modelData
                                width: parent.width
                                wrapMode: Text.Wrap
                                maximumLineCount: 3
                                elide: Text.ElideRight
                                text: "v" + modelData.number + "  " + modelData.date + "  "
                                      + (modelData.changelog !== "" ? modelData.changelog : "(no notes)")
                                      + (modelData.needs !== "" ? "  — needs Janus " + modelData.needs : "")
                                color: Theme.dim
                                font.pixelSize: Theme.px(17)
                            }
                        }
                    }
                }

                // Several games.
                Repeater {
                    model: root.hub.confirm && root.hub.confirm.all ? root.hub.confirm.items : []
                    delegate: Column {
                        required property var modelData
                        width: confirmCol.width
                        spacing: Theme.px(4)
                        Text {
                            width: parent.width
                            wrapMode: Text.Wrap
                            text: modelData.name + "  v" + modelData.version + "  by " + modelData.uploader
                            color: Theme.text
                            font.pixelSize: Theme.px(22)
                        }
                        Text {
                            width: parent.width
                            wrapMode: Text.Wrap
                            visible: text !== ""
                            text: modelData.changelog
                            color: Theme.dim
                            font.pixelSize: Theme.px(19)
                            maximumLineCount: 4
                            elide: Text.ElideRight
                        }
                        Text {
                            text: modelData.url
                            color: Theme.accent
                            font.pixelSize: Theme.px(18)
                        }
                    }
                }
                Text {
                    visible: !confirmView.removing
                    width: confirmCol.width
                    topPadding: Theme.px(10)
                    wrapMode: Text.Wrap
                    text: "Handlers are scripts that run with access to your files and programs. "
                          + "Anyone can read their source on the hub page."
                    color: Theme.warning
                    font.pixelSize: Theme.px(19)
                }
            }
        }

        Text {
            id: statusLine
            anchors { left: parent.left; right: parent.right; bottom: hints.top; margins: Theme.px(32) }
            wrapMode: Text.Wrap
            text: root.hub.busy !== "" ? root.hub.busy
                  : root.hub.error !== "" ? root.hub.error : root.hub.notice
            color: root.hub.busy === "" && root.hub.error !== "" ? Theme.danger : Theme.dim
            font.pixelSize: Theme.px(19)
        }

        Row {
            id: hints
            anchors { left: parent.left; bottom: parent.bottom; margins: Theme.px(32) }
            spacing: Theme.px(36)
            ButtonHint { btn: "updown"; label: "Choose"; visible: !root.editing && !root.hub.confirm && !inputMode.mouse }
            ButtonHint { btn: "ltrt"; label: "Sort"; visible: !root.editing && !root.hub.confirm && !inputMode.mouse }
            ButtonHint {
                btn: "a"
                label: root.editing ? "Type"
                       : !root.hub.confirm ? "Select"
                       : confirmView.removing ? "Remove"
                       : !confirmView.one || confirmView.one.have === 0 ? "Install"
                       : confirmView.one.have < confirmView.one.version ? "Update" : "Reinstall"
                padOnly: root.editing || !root.hub.confirm    // the mouse clicks rows
            }
            ButtonHint {
                btn: "x"; label: root.editing ? "Delete" : "Search"; padOnly: root.editing
                visible: !root.hub.confirm && !(inputMode.mouse && padOnly)
            }
            ButtonHint {
                btn: "x"; label: "Remove handler"
                visible: !!confirmView.one && confirmView.one.have > 0 && !confirmView.removing
            }
            ButtonHint { btn: "y"; label: "Shift"; padOnly: true; visible: root.editing && !inputMode.mouse }
            ButtonHint { btn: "start"; label: "Search"; visible: root.editing; key: "Enter" }
            ButtonHint { btn: "b"; label: confirmView.removing ? "Back" : root.editing || root.hub.confirm ? "Cancel" : "Close" }
        }
    }
}
