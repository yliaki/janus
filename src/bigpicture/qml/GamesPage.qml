import QtQuick

// Cover-art grid of the games Janus can play: those with a handler (also
// when the game isn't installed yet: "Not installed") and installed ones
// whose handler is on the hub ("Get handler"). Games with no handler here or
// on the hub aren't shown. Y gets handlers from the hub (cards whose hub
// handler has a newer version say "Update", checked at startup), Back
// rescans the Steam library, B quits (after a yes/no).
Item {
    id: root
    signal chosen(var appid, string name)   // Steam appid, or a non-Steam game id
    signal keysReleased()       // the hub panel's search box gave the keyboard back

    property bool confirmQuit: false
    property bool quitYes: false
    property bool rebuilding: false     // the grid is being refilled: no animations

    // Rescan, keeping the highlighted game. The model only resets when
    // something changed; then the covers come back without re-animating.
    function refresh() {
        const appid = grid.currentItem ? grid.currentItem.appid : -1
        rebuilding = true
        backend.refreshGames()
        grid.currentIndex = Math.max(0, backend.games.indexOf(appid))
        Qt.callLater(() => {
            rebuilding = false
            showHero(heroSrc)
            scrollToCurrent()
        })
    }

    // One wheel notch = one row, like ▲▼. Not while a menu or dialog is open.
    function wheel(steps) {
        if (hubPanel.visible || confirmQuit) return
        if (steps > 0) grid.moveCurrentIndexDown(); else grid.moveCurrentIndexUp()
        scrollToCurrent()
    }

    // Mouse mode doesn't auto-scroll to the selection (hovering a cover at the
    // edge would scroll the grid under the cursor): the wheel scrolls to it
    // explicitly, just enough to show the whole row.
    function scrollToCurrent() {
        if (grid.highlightRangeMode !== GridView.NoHighlightRange) return
        const from = grid.contentY
        grid.positionViewAtIndex(grid.currentIndex, GridView.Contain)
        const to = grid.contentY
        grid.contentY = from
        scrollAnim.to = to
        scrollAnim.restart()
    }
    NumberAnimation { id: scrollAnim; target: grid; property: "contentY"; duration: 150; easing.type: Easing.OutQuad }

    function handle(btn, js) {
        if (settings.visible) { settings.handle(btn, js); return }
        if (hubPanel.visible) {
            hubPanel.handle(btn, js)
            if (!hubPanel.visible) refresh()
            return
        }
        if (confirmQuit) {
            switch (btn) {
            case "left": case "right": quitYes = !quitYes; break
            case "a": if (quitYes) Qt.quit(); else confirmQuit = false; break
            case "b": confirmQuit = false; break
            }
            return
        }
        switch (btn) {
        // In mouse/keyboard mode the grid doesn't follow its selection by
        // itself (hover must never scroll it): arrow keys scroll explicitly.
        case "left": grid.moveCurrentIndexLeft(); scrollToCurrent(); break
        case "right": grid.moveCurrentIndexRight(); scrollToCurrent(); break
        case "up": grid.moveCurrentIndexUp(); scrollToCurrent(); break
        case "down": grid.moveCurrentIndexDown(); scrollToCurrent(); break
        case "a":
            if (!grid.currentItem) break
            if (!grid.currentItem.installed)
                backend.installInSteam(grid.currentItem.appid)
            else if (!grid.currentItem.handler)
                hubPanel.openFor(grid.currentItem.appid, grid.currentItem.name)
            else
                root.chosen(grid.currentItem.appid, grid.currentItem.name)
            break
        case "y": hubPanel.open(); break
        case "start": settings.open(); break
        case "back": refresh(); break
        case "b": quitYes = false; confirmQuit = true; break
        }
    }

    // Banner of the highlighted game behind the grid, cross-fading as the
    // selection moves.
    readonly property string heroSrc: grid.currentItem ? grid.currentItem.hero : ""
    property bool showA: true
    onHeroSrcChanged: if (!rebuilding) showHero(heroSrc)
    function showHero(src) {
        if ((showA ? heroA : heroB).source.toString() === src) return
        if (showA) heroB.source = src; else heroA.source = src
        showA = !showA
    }
    component HeroImage: Image {
        property bool shown
        anchors { left: parent.left; right: parent.right; top: parent.top }
        height: Math.min(root.height * 0.75, width / 3.1)
        fillMode: Image.PreserveAspectCrop
        verticalAlignment: Image.AlignTop
        asynchronous: true
        sourceSize.width: 1920
        opacity: shown && status === Image.Ready ? 0.45 : 0
        Behavior on opacity { NumberAnimation { duration: 350 } }
    }
    HeroImage { id: heroA; shown: root.showA }
    HeroImage { id: heroB; shown: !root.showA }
    Rectangle {
        anchors { left: parent.left; right: parent.right; top: parent.top }
        height: Math.min(root.height * 0.75, width / 3.1) + 2
        gradient: Gradient {
            GradientStop { position: 0.0; color: Qt.rgba(Theme.bg.r, Theme.bg.g, Theme.bg.b, 0.2) }
            GradientStop { position: 0.6; color: Qt.rgba(Theme.bg.r, Theme.bg.g, Theme.bg.b, 0.75) }
            GradientStop { position: 1.0; color: Theme.bg }
        }
    }

    Text {
        id: title
        anchors { left: parent.left; top: parent.top; margins: Theme.px(56) }
        text: "Janus Split Screen"
        color: Theme.text
        font.pixelSize: Theme.px(48)
        font.bold: true
    }

    GridView {
        id: grid
        readonly property int cardW: Theme.px(230)
        readonly property int cardH: Math.round(cardW * 1.5)
        cellWidth: cardW + Theme.px(36)
        cellHeight: cardH + Theme.px(80)
        width: Math.max(cellWidth, Math.floor((parent.width - Theme.px(112)) / cellWidth) * cellWidth)
        anchors {
            top: title.bottom; bottom: footer.top
            topMargin: Theme.px(28); bottomMargin: Theme.px(12)
            horizontalCenter: parent.horizontalCenter
        }
        clip: true
        model: backend.games
        // Pad/keyboard: the view follows the selection. Mouse: it doesn't
        // (hover selects, and must never scroll; see scrollToCurrent()).
        highlightRangeMode: inputMode.mouse ? GridView.NoHighlightRange : GridView.ApplyRange
        highlightFollowsCurrentItem: !inputMode.mouse
        preferredHighlightBegin: Theme.px(30)
        preferredHighlightEnd: height - Theme.px(30)
        highlightMoveDuration: root.rebuilding ? 0 : 150
        keyNavigationEnabled: false
        onCountChanged: if (currentIndex < 0 && count > 0) currentIndex = 0

        delegate: Item {
            id: card
            required property int index
            required property var appid
            required property string name
            required property string cover
            required property bool portrait
            required property bool known
            required property string hero
            required property bool installed
            required property bool handler
            readonly property bool current: GridView.isCurrentItem
            readonly property bool update: backend.hub.updates.indexOf(appid) >= 0

            width: grid.cellWidth
            height: grid.cellHeight
            z: current ? 1 : 0

            // Moving over a card selects it (not scrolling under a resting
            // cursor), clicking opens it like A.
            MouseArea {
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onPositionChanged: if (inputMode.mouse) grid.currentIndex = card.index
                onClicked: {
                    grid.currentIndex = card.index
                    root.handle("a", -1)
                }
            }

            Rectangle {
                id: art
                width: grid.cardW
                height: grid.cardH
                anchors.horizontalCenter: parent.horizontalCenter
                y: Theme.px(14)
                radius: Theme.px(10)
                color: Theme.panel2
                clip: true
                scale: card.current ? 1.07 : 1.0
                Behavior on scale { enabled: !root.rebuilding; NumberAnimation { duration: 120 } }

                Image {
                    anchors.fill: parent
                    opacity: card.installed && card.handler ? 1 : 0.35
                    source: card.cover
                    fillMode: card.portrait ? Image.PreserveAspectCrop : Image.PreserveAspectFit
                    asynchronous: true
                    sourceSize.width: grid.cardW * 2
                }
                Text {
                    visible: card.cover === ""
                    anchors.fill: parent
                    anchors.margins: Theme.px(16)
                    text: card.name
                    color: Theme.text
                    wrapMode: Text.Wrap
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                    font.pixelSize: Theme.px(24)
                    font.bold: true
                }
                Rectangle {
                    // a handler but no game, or the game but no handler
                    visible: !card.installed || !card.handler
                    anchors { left: parent.left; right: parent.right; bottom: parent.bottom }
                    height: Theme.px(44)
                    color: "#c0000000"
                    Text {
                        anchors.centerIn: parent
                        text: card.installed ? "Get handler" : "Not installed"
                        color: Theme.warning
                        font.pixelSize: Theme.px(19)
                        font.bold: true
                    }
                }
                Rectangle {
                    // the hub has a newer version of this game's handler
                    visible: card.update
                    anchors { left: parent.left; top: parent.top; margins: Theme.px(10) }
                    width: updText.implicitWidth + Theme.px(20)
                    height: Theme.px(32)
                    radius: Theme.px(8)
                    color: Theme.warning
                    Text {
                        id: updText
                        anchors.centerIn: parent
                        text: "Update"
                        color: Theme.bg
                        font.pixelSize: Theme.px(17)
                        font.bold: true
                    }
                }
                Rectangle {
                    anchors.fill: parent
                    radius: parent.radius
                    color: "transparent"
                    border.width: card.current ? Theme.px(4) : 0
                    border.color: Theme.text
                }
            }
            Text {
                anchors { top: art.bottom; topMargin: Theme.px(18); horizontalCenter: parent.horizontalCenter }
                width: grid.cardW
                text: card.name
                elide: Text.ElideRight
                horizontalAlignment: Text.AlignHCenter
                color: card.current ? Theme.text : Theme.dim
                font.pixelSize: Theme.px(20)
                font.bold: card.current
            }
        }
    }

    Text {
        anchors.centerIn: parent
        visible: grid.count === 0
        width: parent.width * 0.6
        horizontalAlignment: Text.AlignHCenter
        wrapMode: Text.Wrap
        text: "None of your games has a Janus handler yet.\n"
              + (inputMode.mouse ? "Click Get handlers" : "Press Y") + " to find some on the handler hub."
        color: Theme.dim
        font.pixelSize: Theme.px(28)
    }

    Row {
        id: footer
        anchors { right: parent.right; bottom: parent.bottom; margins: Theme.px(40) }
        spacing: Theme.px(40)
        ButtonHint {
            btn: "a"
            label: !grid.currentItem ? "Select"
                   : !grid.currentItem.installed ? "Install in Steam"
                   : !grid.currentItem.handler ? "Get handler" : "Select"
        }
        ButtonHint {
            btn: "y"
            label: "Get handlers" + (backend.hub.updateCount > 0
                   ? " (" + backend.hub.updateCount + " update" + (backend.hub.updateCount === 1 ? ")" : "s)") : "")
        }
        ButtonHint { btn: "start"; label: "Settings" }
        ButtonHint { btn: "back"; label: "Refresh" }
        ButtonHint { btn: "b"; label: "Quit" }
    }

    // What A does for a game that can't be played yet: up by the title, where
    // there's room (by the footer it ran into the hints, which grow wide in
    // mouse/keyboard mode).
    Text {
        anchors {
            right: parent.right; rightMargin: Theme.px(56)
            verticalCenter: title.verticalCenter
        }
        visible: grid.currentItem !== null && (!grid.currentItem.installed || !grid.currentItem.handler)
        width: Math.min(Theme.px(720), parent.width - title.width - Theme.px(56 * 2 + 360))
        horizontalAlignment: Text.AlignRight
        maximumLineCount: 2
        elide: Text.ElideRight
        wrapMode: Text.Wrap
        text: !grid.currentItem ? ""
              : !grid.currentItem.installed
              ? "Janus has a handler for this game, but it isn't installed. "
                + "Install it in Steam, then " + (inputMode.mouse ? "refresh." : "press Back to refresh.")
              : "The handler hub has a handler for this game. "
                + (inputMode.mouse ? "Click it" : "Press A") + " to get it."
        color: Theme.dim
        font.pixelSize: Theme.px(19)
    }

    HubPanel {
        id: hubPanel
        anchors.fill: parent
        onKeysReleased: root.keysReleased()
    }

    // Start: settings that aren't per game (controller setup and mappings).
    OptionsPanel {
        id: settings
        anchors.fill: parent
        opts: backend.globalOptions
        title: "Settings"
        onKeysReleased: root.keysReleased()
    }

    // B: quit, after a yes/no.
    Rectangle {
        anchors.fill: parent
        visible: root.confirmQuit
        color: "#c0000000"
        MouseArea { anchors.fill: parent; onClicked: root.confirmQuit = false }
        Rectangle {
            anchors.centerIn: parent
            width: Theme.px(560)
            height: quitCol.implicitHeight + Theme.px(64)
            radius: Theme.px(16)
            color: Theme.panel
            border.color: Theme.accent
            border.width: Theme.px(2)
            MouseArea { anchors.fill: parent }    // clicks in the box don't cancel
            Column {
                id: quitCol
                anchors.centerIn: parent
                spacing: Theme.px(28)
                Text {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: "Quit Janus?"
                    color: Theme.text
                    font.pixelSize: Theme.px(32)
                    font.bold: true
                }
                Row {
                    anchors.horizontalCenter: parent.horizontalCenter
                    spacing: Theme.px(24)
                    Repeater {
                        model: [{ label: "Yes", yes: true }, { label: "No", yes: false }]
                        delegate: Rectangle {
                            id: choice
                            required property var modelData
                            readonly property bool sel: modelData.yes === root.quitYes
                            MouseArea {
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onEntered: root.quitYes = choice.modelData.yes
                                onClicked: {
                                    root.quitYes = choice.modelData.yes
                                    root.handle("a", -1)
                                }
                            }
                            width: Theme.px(180); height: Theme.px(64)
                            radius: Theme.px(10)
                            color: sel ? Theme.panel2 : "transparent"
                            border.color: sel ? Theme.accent : Theme.line
                            border.width: Theme.px(2)
                            Text {
                                anchors.centerIn: parent
                                text: modelData.label
                                color: Theme.text
                                font.pixelSize: Theme.px(24)
                                font.bold: parent.sel
                            }
                        }
                    }
                }
                Row {
                    anchors.horizontalCenter: parent.horizontalCenter
                    spacing: Theme.px(36)
                    visible: !inputMode.mouse
                    ButtonHint { btn: "leftright"; label: "Choose" }
                    ButtonHint { btn: "a"; label: "OK" }
                    ButtonHint { btn: "b"; label: "Cancel" }
                }
            }
        }
    }
}
