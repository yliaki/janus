import QtQuick

// Pads join with A. Each joined pad moves its own tile with the D-pad,
// changes its own player number with LB/RB and picks who's playing (their
// profile: name + saves) with X; LT/RT picks the layout.
// P1 is instance 1 (the host for Goldberg auto-join); saves follow the
// profile, whatever the number. With the mouse: click a pad to add/remove it,
// drag a screen onto another, ◀ ▶ on a screen for the number, click the
// name for who's playing, ◀ ▶ around the layout name for the layout.
// Any button on a pad flashes its pill and its player's tile (PadFlash).
Item {
    id: root
    signal back()
    signal keysReleased()

    // ◀ / ▶ the mouse clicks (player number, layout).
    component ClickArrow: Text {
        id: arrow
        property int delta
        property int size: 26
        signal clicked()
        text: delta < 0 ? "◀" : "▶"
        color: area.containsMouse ? Theme.text : Theme.dim
        font.pixelSize: Theme.px(size)
        MouseArea {
            id: area
            anchors { fill: parent; margins: -Theme.px(10) }
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: arrow.clicked()
        }
    }

    function handle(btn, js) {
        if (options.visible) { options.handle(btn, js); return }
        if (profilePanel.visible) { profilePanel.handle(btn, js); return }
        const joined = js >= 0 && backend.isJoined(js)
        switch (btn) {
        case "a": backend.join(js); break
        case "b":
            if (joined) backend.leave(js)
            else root.back()
            break
        case "up": case "down": case "left": case "right":
            if (joined) backend.moveSeat(js, btn)
            break
        case "x":
            if (joined) {
                const t = backend.previewTiles.find(t => t.js === js)
                profilePanel.open(js, t ? t.number : 0)
            }
            break
        case "lb": if (joined) backend.changeNumber(js, -1); break
        case "rb": if (joined) backend.changeNumber(js, 1); break
        case "lt": backend.cycleLayout(-1); break
        case "rt": backend.cycleLayout(1); break
        case "y": if (backend.options.length > 0) options.open(); break
        case "start": backend.launch(); break
        }
    }

    // The game's wide Steam banner behind the top of the screen, fading
    // into the background.
    Item {
        id: hero
        anchors { left: parent.left; right: parent.right; top: parent.top }
        height: Math.min(parent.height * 0.62, width / 3.1)
        visible: backend.heroUrl !== ""
        Image {
            anchors.fill: parent
            source: backend.heroUrl
            fillMode: Image.PreserveAspectCrop
            verticalAlignment: Image.AlignTop
            asynchronous: true
            sourceSize.width: 1920
            opacity: 0.6
        }
        Rectangle {
            anchors.fill: parent
            gradient: Gradient {
                GradientStop { position: 0.0; color: Qt.rgba(Theme.bg.r, Theme.bg.g, Theme.bg.b, 0.25) }
                GradientStop { position: 0.55; color: Qt.rgba(Theme.bg.r, Theme.bg.g, Theme.bg.b, 0.7) }
                GradientStop { position: 1.0; color: Theme.bg }
            }
        }
    }

    // Title: the game's logo when Steam has one, else its name.
    Item {
        id: title
        anchors { left: parent.left; top: parent.top; margins: Theme.px(56) }
        width: parent.width - Theme.px(112)
        height: logo.visible ? Theme.px(110) : nameText.implicitHeight
        Image {
            id: logo
            visible: backend.logoUrl !== "" && status === Image.Ready
            anchors { left: parent.left; top: parent.top; bottom: parent.bottom }
            width: Math.min(sourceSize.width * height / Math.max(1, sourceSize.height), parent.width * 0.45)
            source: backend.logoUrl
            fillMode: Image.PreserveAspectFit
            horizontalAlignment: Image.AlignLeft
            asynchronous: true
        }
        Text {
            id: nameText
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
        id: subtitle
        anchors { left: title.left; top: title.bottom; topMargin: Theme.px(14) }
        // Readable over a busy banner.
        style: Text.Raised
        styleColor: "#cc000000"
        text: inputMode.mouse
              ? (backend.playerCount === 0
                 ? "Click each controller below that wants to play (or press A on it)"
                 : "Drag a screen to move it  ·  ◀ ▶ player number  ·  click the name for who's playing")
              : backend.playerCount === 0
              ? "Press A on each controller that wants to play"
              : "✚ move your screen  ·  LB / RB change your player number  ·  X who's playing  ·  Start to play"
        color: Theme.dim
        font.pixelSize: Theme.px(24)
    }

    // All monitors, with the layout's tiles on top.
    Item {
        id: stage
        anchors {
            top: subtitle.bottom; bottom: picker.top
            left: parent.left; right: parent.right
            topMargin: Theme.px(36); bottomMargin: Theme.px(24)
            leftMargin: Theme.px(56); rightMargin: Theme.px(56)
        }
        Item {
            id: desk
            readonly property real aspect: backend.previewAspect
            property int dropSeat: -1          // tile under a dragged screen

            // The tile index at desk point (x, y), or -1.
            function seatAt(x, y) {
                const ts = backend.previewTiles
                for (let i = 0; i < ts.length; i++) {
                    const t = ts[i]
                    if (x >= t.x * width && x < (t.x + t.w) * width
                            && y >= t.y * height && y < (t.y + t.h) * height)
                        return i
                }
                return -1
            }
            width: Math.min(stage.width, stage.height * aspect)
            height: width / aspect
            anchors.centerIn: parent

            Repeater {
                model: backend.previewMonitors
                delegate: Rectangle {
                    required property var modelData
                    readonly property int gap: Theme.px(4)
                    x: modelData.x * desk.width + gap
                    y: modelData.y * desk.height + gap
                    width: modelData.w * desk.width - 2 * gap
                    height: modelData.h * desk.height - 2 * gap
                    color: Theme.panel
                    opacity: modelData.used ? 1 : 0.45
                    radius: Theme.px(12)
                    border.color: Theme.line
                    border.width: Theme.px(2)
                    Text {
                        visible: backend.previewMonitors.length > 1
                        anchors { bottom: parent.bottom; horizontalCenter: parent.horizontalCenter; bottomMargin: -Theme.px(30) }
                        text: parent.modelData.name
                        color: Theme.dim
                        font.pixelSize: Theme.px(18)
                    }
                }
            }

            Repeater {
                model: backend.previewTiles
                delegate: Item {
                    id: tile
                    required property var modelData
                    required property int index
                    readonly property bool taken: modelData.number > 0
                    readonly property bool mine: taken && modelData.js >= 0   // mouse can act on it
                    z: drag.active ? 10 : 0
                    transform: Translate { x: drag.dx; y: drag.dy }
                    readonly property color pcolor: taken ? Theme.players[(modelData.number - 1) % Theme.players.length] : Theme.line
                    readonly property int gap: Theme.px(10)
                    x: modelData.x * desk.width + gap
                    y: modelData.y * desk.height + gap
                    width: modelData.w * desk.width - 2 * gap
                    height: modelData.h * desk.height - 2 * gap
                    Behavior on x { NumberAnimation { duration: 120 } }
                    Behavior on y { NumberAnimation { duration: 120 } }

                    Rectangle {
                        anchors.fill: parent
                        radius: Theme.px(8)
                        color: tile.taken ? Qt.darker(tile.pcolor, 2.6) : Theme.panel2
                        border.color: desk.dropSeat === tile.index && !drag.active ? Theme.text : tile.pcolor
                        border.width: Theme.px(desk.dropSeat === tile.index && !drag.active ? 5 : 3)
                        opacity: tile.modelData.reconnecting ? 0.5 : drag.active ? 0.85 : 1
                        PadFlash { js: tile.modelData.js }
                    }

                    // Drag a screen onto another tile to move there (swapping).
                    MouseArea {
                        id: drag
                        property real dx: 0
                        property real dy: 0
                        property point start
                        readonly property bool active: pressed && (Math.abs(dx) + Math.abs(dy) > Theme.px(8))
                        anchors.fill: parent
                        enabled: tile.mine
                        cursorShape: active ? Qt.ClosedHandCursor : Qt.OpenHandCursor
                        onPressed: (m) => { start = mapToItem(desk, m.x, m.y); dx = 0; dy = 0 }
                        onPositionChanged: (m) => {
                            const p = mapToItem(desk, m.x, m.y)
                            // dx/dy move the tile, so measure in desk coordinates
                            dx = p.x - start.x
                            dy = p.y - start.y
                            desk.dropSeat = active ? desk.seatAt(p.x, p.y) : -1
                        }
                        onReleased: {
                            // Reset first: the move rebuilds this delegate.
                            const seat = active ? desk.dropSeat : -1
                            const js = tile.modelData.js
                            dx = 0; dy = 0
                            desk.dropSeat = -1
                            if (seat >= 0) backend.moveSeatTo(js, seat)
                        }
                        onCanceled: { dx = 0; dy = 0; desk.dropSeat = -1 }
                    }

                    // Remove this player (mouse).
                    Rectangle {
                        visible: inputMode.mouse && tile.mine
                        anchors { top: parent.top; left: parent.left; margins: Theme.px(10) }
                        width: Theme.px(34); height: width; radius: width / 2
                        color: leaveArea.containsMouse ? Theme.danger : Theme.panel
                        border.color: Theme.line; border.width: Theme.px(2)
                        Text { anchors.centerIn: parent; text: "✕"; color: Theme.text; font.pixelSize: Theme.px(16) }
                        MouseArea {
                            id: leaveArea
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: backend.leave(tile.modelData.js)
                        }
                    }
                    Column {
                        anchors.centerIn: parent
                        width: parent.width - Theme.px(24)
                        spacing: Theme.px(8)
                        // "P1", with ◀ ▶ to change the number by mouse.
                        Row {
                            anchors.horizontalCenter: parent.horizontalCenter
                            spacing: Theme.px(18)
                            readonly property bool arrows: inputMode.mouse && tile.mine && backend.playerCount > 1
                            ClickArrow {
                                delta: -1; size: 30
                                visible: parent.arrows
                                anchors.verticalCenter: parent.verticalCenter
                                onClicked: backend.changeNumber(tile.modelData.js, delta)
                            }
                            Text {
                                text: tile.taken ? "P" + tile.modelData.number
                                      : inputMode.mouse ? "Click a controller below to join" : "Press A to join"
                                color: tile.taken ? tile.pcolor : Theme.dim
                                font.pixelSize: tile.taken ? Theme.px(56) : Theme.px(26)
                                font.bold: tile.taken
                                width: tile.taken ? implicitWidth : tile.width - Theme.px(24)
                                horizontalAlignment: Text.AlignHCenter
                                wrapMode: Text.Wrap
                            }
                            ClickArrow {
                                delta: 1; size: 30
                                visible: parent.arrows
                                anchors.verticalCenter: parent.verticalCenter
                                onClicked: backend.changeNumber(tile.modelData.js, delta)
                            }
                        }
                        // The profile; click it for who's playing.
                        Text {
                            visible: tile.taken
                            width: parent.width
                            horizontalAlignment: Text.AlignHCenter
                            elide: Text.ElideRight
                            text: tile.modelData.profile + (inputMode.mouse && tile.mine ? "  ▾" : "")
                            color: Theme.text
                            font.pixelSize: Theme.px(30)
                            font.bold: true
                            font.underline: profArea.containsMouse
                            MouseArea {
                                id: profArea
                                anchors.centerIn: parent
                                width: Math.min(parent.width, parent.contentWidth + Theme.px(20))
                                height: parent.height
                                enabled: tile.mine
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: profilePanel.open(tile.modelData.js, tile.modelData.number)
                            }
                        }
                        Text {
                            visible: tile.taken
                            width: parent.width
                            horizontalAlignment: Text.AlignHCenter
                            elide: Text.ElideRight
                            text: tile.modelData.name
                            color: Theme.dim
                            font.pixelSize: Theme.px(18)
                        }
                        Text {
                            visible: tile.modelData.reconnecting
                            width: parent.width
                            horizontalAlignment: Text.AlignHCenter
                            text: "Controller disconnected — reconnecting…"
                            color: Theme.warning
                            font.pixelSize: Theme.px(18)
                            wrapMode: Text.Wrap
                            SequentialAnimation on opacity {
                                running: tile.modelData.reconnecting
                                loops: Animation.Infinite
                                alwaysRunToEnd: true
                                NumberAnimation { to: 0.35; duration: 600 }
                                NumberAnimation { to: 1.0; duration: 600 }
                            }
                        }
                        Text {
                            visible: tile.modelData.saves === "real" || tile.modelData.saves === "shared"
                            width: parent.width
                            horizontalAlignment: Text.AlignHCenter
                            text: tile.modelData.saves === "real" ? "Real Steam saves" : "Shared saves"
                            color: Theme.dim
                            font.pixelSize: Theme.px(17)
                        }
                    }
                    // Just set their own number: others shift around them.
                    Rectangle {
                        visible: tile.modelData.pinned
                        anchors { top: parent.top; right: parent.right; margins: Theme.px(10) }
                        width: Theme.px(30); height: width; radius: width / 2
                        color: tile.pcolor
                        Text {
                            anchors.centerIn: parent
                            text: "✓"
                            color: Theme.bg
                            font.pixelSize: Theme.px(18)
                            font.bold: true
                        }
                    }
                }
            }
        }
    }

    // Layout picker.
    Row {
        id: picker
        anchors { bottom: pads.top; bottomMargin: Theme.px(28); horizontalCenter: parent.horizontalCenter }
        spacing: Theme.px(18)
        ButtonHint { btn: "lt"; label: ""; visible: backend.layoutCount > 1 && !inputMode.mouse }
        ClickArrow {
            delta: -1
            visible: inputMode.mouse && backend.layoutCount > 1
            anchors.verticalCenter: parent.verticalCenter
            onClicked: backend.cycleLayout(delta)
        }
        Text {
            anchors.verticalCenter: parent.verticalCenter
            text: backend.layoutName
            color: Theme.text
            font.pixelSize: Theme.px(26)
            font.bold: true
        }
        ClickArrow {
            delta: 1
            visible: inputMode.mouse && backend.layoutCount > 1
            anchors.verticalCenter: parent.verticalCenter
            onClicked: backend.cycleLayout(delta)
        }
        ButtonHint { btn: "rt"; label: ""; visible: backend.layoutCount > 1 && !inputMode.mouse }
    }

    // Connected pads, colored by the player number they have.
    Row {
        id: pads
        anchors { bottom: footer.top; bottomMargin: Theme.px(28); horizontalCenter: parent.horizontalCenter }
        spacing: Theme.px(16)
        // Up to 8 pads: shrink the row rather than run off the screen.
        transformOrigin: Item.Bottom
        scale: Math.min(1, (parent.width - Theme.px(80)) / Math.max(1, implicitWidth))
        Repeater {
            model: backend.pads
            delegate: Rectangle {
                id: pill
                required property var modelData
                readonly property bool joined: modelData.number > 0
                // Click: add this pad as a player, or remove it.
                MouseArea {
                    id: pillArea
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: pill.joined ? backend.leave(pill.modelData.js)
                                           : backend.join(pill.modelData.js)
                }
                scale: pillArea.containsMouse ? 1.06 : 1
                readonly property color pcolor: joined ? Theme.players[(modelData.number - 1) % Theme.players.length] : Theme.line
                height: Theme.px(44)
                width: padLabel.implicitWidth + Theme.px(36)
                radius: height / 2
                color: joined ? Qt.darker(pcolor, 2.6) : Theme.panel
                border.color: pcolor
                border.width: Theme.px(2)
                PadFlash { js: pill.modelData.js }
                Row {
                    id: padLabel
                    anchors.centerIn: parent
                    spacing: Theme.px(12)
                    Text {
                        anchors.verticalCenter: parent.verticalCenter
                        text: (padLabel.parent.joined ? "P" + padLabel.parent.modelData.number + "  " : "") + padLabel.parent.modelData.name
                        color: padLabel.parent.joined ? Theme.text : Theme.dim
                        font.pixelSize: Theme.px(18)
                    }
                    // Battery of a wireless pad (hidden when it reports none).
                    Row {
                        readonly property int level: padLabel.parent.modelData.battery
                        visible: level >= 0
                        anchors.verticalCenter: parent.verticalCenter
                        spacing: Theme.px(2)
                        Rectangle {
                            width: Theme.px(26); height: Theme.px(13); radius: Theme.px(3)
                            color: "transparent"
                            border.color: Theme.dim; border.width: Theme.px(2)
                            Rectangle {
                                x: Theme.px(3); y: Theme.px(3)
                                height: parent.height - Theme.px(6)
                                width: Math.max(Theme.px(2), (parent.width - Theme.px(6)) * parent.parent.level / 100)
                                color: parent.parent.level <= 20 ? Theme.danger : Theme.text
                            }
                        }
                        Rectangle {
                            width: Theme.px(3); height: Theme.px(6)
                            anchors.verticalCenter: parent.verticalCenter
                            color: Theme.dim
                        }
                    }
                }
            }
        }
        Text {
            visible: backend.pads.length === 0
            text: "No controllers connected"
            color: Theme.dim
            font.pixelSize: Theme.px(20)
        }
    }

    // Pads SDL has no mapping for do nothing in Janus input: say so.
    Text {
        anchors { top: pads.bottom; topMargin: Theme.px(4); horizontalCenter: parent.horizontalCenter }
        visible: gamepad.unknownPads.length > 0
        text: "Not recognised: " + gamepad.unknownPads.map(p => p.name).join(", ")
              + "  ·  Options → Set up a controller"
        color: Theme.warning
        font.pixelSize: Theme.px(18)
    }

    Row {
        id: footer
        anchors { right: parent.right; bottom: parent.bottom; margins: Theme.px(40) }
        spacing: Theme.px(34)
        ButtonHint { btn: "a"; label: "Join"; visible: !inputMode.mouse }
        ButtonHint { btn: "b"; label: inputMode.mouse ? "Back" : "Leave / Back" }
        ButtonHint { btn: "dpad"; label: "Move"; visible: backend.playerCount > 1 && !inputMode.mouse }
        ButtonHint { btn: "lbrb"; label: "Player number"; visible: backend.playerCount > 1 && !inputMode.mouse }
        ButtonHint { btn: "x"; label: "Who's playing"; visible: backend.playerCount > 0 && !inputMode.mouse }
        ButtonHint { btn: "ltrt"; label: "Layout"; visible: backend.layoutCount > 1 && !inputMode.mouse }
        ButtonHint { btn: "y"; label: "Options"; visible: backend.options.length > 0 }
        ButtonHint { btn: "start"; label: "Play"; visible: backend.playerCount > 0 }
    }

    OptionsPanel {
        id: options
        anchors.fill: parent
        onKeysReleased: root.keysReleased()
    }

    ProfilePanel {
        id: profilePanel
        anchors.fill: parent
        onKeysReleased: root.keysReleased()
    }
}
