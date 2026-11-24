import QtQuick
import QtQuick.Window

Window {
    id: win
    visible: true
    width: 1600
    height: 900
    visibility: windowed ? Window.Windowed : Window.FullScreen
    color: Theme.bg
    title: "Janus Split Screen"

    // games -> players -> running
    property string page: "games"

    onHeightChanged: Theme.s = Math.max(0.5, height / 1080)
    Component.onCompleted: Theme.s = Math.max(0.5, height / 1080)

    // The pads that play the game must not drive the launcher behind it.
    Binding { target: gamepad; property: "enabled"; value: win.active }
    // Back from Steam (maybe a game was installed meanwhile): rescan.
    onActiveChanged: if (active && page === "games") gamesPage.refresh()

    function dispatch(btn, js) {
        if (backend.messageText !== "") {
            // The handler's launch message (or a warning before the launch):
            // A plays, B goes back. Start doesn't count, so a pad mashing Start
            // can't skip it unread.
            // A non-Steam game's setup step works the same (A = its fix);
            // nothing dismisses a running action ("busy").
            const lvl = backend.messageLevel
            if (lvl === "busy") return
            if (lvl === "launch" || lvl === "launch-warning" || lvl === "setup" || lvl === "choice" || lvl === "remove") {
                if (btn === "a") backend.dismissMessage()
                else if (btn === "b") backend.cancelMessage()
            } else if (btn === "a" || btn === "b" || btn === "start")
                backend.dismissMessage()
            return
        }
        if (padSetup.active) { padSetup.handle(btn, js); return }
        if (page === "games") gamesPage.handle(btn, js)
        else if (page === "players") playersPage.handle(btn, js)
        else runningPage.handle(btn, js)
    }

    Connections {
        target: gamepad
        function onButton(name, js) {
            inputMode.mouse = false
            win.dispatch(name, js)
        }
    }
    // A clicked button hint: the same as its key.
    Connections {
        target: inputMode
        function onPressed(btn) { win.dispatch(btn, -1) }
        // The wheel steps through the games; nothing else scrolls by wheel.
        function onWheel(steps) {
            if (page === "games" && backend.messageText === "") gamesPage.wheel(steps)
        }
    }

    Connections {
        target: backend
        function onRunningChanged() {
            if (backend.running) win.page = "running"
        }
        function onLaunchFinished() {
            win.page = "players"
            win.restore()
        }
        function onMessageChanged() {
            if (backend.messageText !== "") win.restore()
        }
        function onSetupRequested() { padSetup.open() }
        function onRemoveMappingRequested(guid, name) { padSetup.askRemove(guid, name) }
        // A non-Steam game's setup was abandoned (B, or a step failed).
        function onLeaveGame() { if (win.page === "players") win.page = "games" }
    }

    function restore() {
        if (windowed) win.showNormal(); else win.showFullScreen()
        // Only KWin can unminimize and focus us on Wayland.
        backend.activateWindow()
    }

    // Keyboard: arrows, Enter = A, Esc = B, Space = Start, X, Y,
    // PageUp/PageDown = LB/RB, [ ] = LT/RT, F5 / Q = Back (View). Keyboard
    // presses have no pad: pads join and move by mouse (or their own buttons).
    Item {
        id: keyRoot
        anchors.fill: parent
        focus: true
        Keys.onPressed: (e) => {
            inputMode.mouse = true
            const map = {
                [Qt.Key_Up]: "up", [Qt.Key_Down]: "down",
                [Qt.Key_Left]: "left", [Qt.Key_Right]: "right",
                [Qt.Key_Return]: "a", [Qt.Key_Enter]: "a",
                [Qt.Key_Escape]: "b", [Qt.Key_Backspace]: "b",
                [Qt.Key_Space]: "start", [Qt.Key_X]: "x",
                [Qt.Key_PageUp]: "lb", [Qt.Key_PageDown]: "rb",
                [Qt.Key_BracketLeft]: "lt", [Qt.Key_BracketRight]: "rt",
                [Qt.Key_Y]: "y",
                [Qt.Key_Q]: "back", [Qt.Key_F5]: "back", [Qt.Key_Delete]: "back",
            }
            if (map[e.key] !== undefined) {
                win.dispatch(map[e.key], -1)
                e.accepted = true
            }
        }
    }

    GamesPage {
        id: gamesPage
        anchors.fill: parent
        visible: win.page === "games"
        onKeysReleased: keyRoot.forceActiveFocus()
        onChosen: (appid, name) => {
            backend.selectGame(appid, name)
            win.page = "players"
        }
    }

    PlayersPage {
        id: playersPage
        anchors.fill: parent
        visible: win.page === "players"
        onBack: win.page = "games"
        // A text option finished editing: give the keyboard back to us.
        onKeysReleased: keyRoot.forceActiveFocus()
    }

    RunningPage {
        id: runningPage
        anchors.fill: parent
        visible: win.page === "running"
    }

    // The controller setup wizard (over the options panel).
    SetupPanel {
        anchors.fill: parent
    }

    // Janus.warn / Janus.error from the handler, and launch errors. The
    // handler hook is blocked until this is dismissed. Level "launch" is the
    // handler's launch message (Game.launchMessage) between Play and the launch;
    // "launch-warning" a warning at that point (e.g. a missing Steam runtime).
    // A non-Steam game: "setup" = a setup step (A runs its fix), "busy" = an
    // action running (its progress, no buttons), "info" = what it did.
    Rectangle {
        id: msgPopup
        readonly property string level: backend.messageLevel
        readonly property bool question: level === "launch" || level === "launch-warning" || level === "setup" || level === "choice" || level === "remove"
        readonly property color tone: level === "error" ? Theme.danger
                                    : level === "launch" || level === "setup" || level === "busy"
                                      || level === "info" ? Theme.accent : Theme.warning
        anchors.fill: parent
        visible: backend.messageText !== ""
        color: "#c0000000"
        MouseArea { anchors.fill: parent }    // nothing behind the popup takes clicks
        Rectangle {
            anchors.centerIn: parent
            width: Math.min(parent.width - Theme.px(80), Theme.px(900))
            height: msgCol.implicitHeight + Theme.px(64)
            radius: Theme.px(16)
            color: Theme.panel
            border.color: msgPopup.tone
            border.width: Theme.px(2)
            Column {
                id: msgCol
                anchors.centerIn: parent
                width: parent.width - Theme.px(64)
                spacing: Theme.px(20)
                Text {
                    width: parent.width
                    text: msgPopup.level === "error" ? "Error"
                        : msgPopup.level === "launch" ? "Before you play " + backend.gameName
                        : msgPopup.level === "setup" ? "Set up " + backend.gameName
                        : msgPopup.level === "choice" ? "Controller input"
                        : msgPopup.level === "remove" ? "Remove a player"
                        : msgPopup.level === "busy" || msgPopup.level === "info" ? backend.gameName
                        : "Warning"
                    color: msgPopup.tone
                    elide: Text.ElideRight
                    font.pixelSize: Theme.px(30)
                    font.bold: true
                }
                Text {
                    width: parent.width
                    text: backend.messageText
                    color: Theme.text
                    wrapMode: Text.Wrap
                    textFormat: Text.PlainText
                    lineHeight: 1.15
                    font.pixelSize: Theme.px(msgPopup.level === "launch" ? 26 : 22)
                }
                Row {
                    spacing: Theme.px(32)
                    ButtonHint {
                        btn: "a"
                        visible: msgPopup.level !== "busy"
                        label: msgPopup.level === "launch" ? "Play"
                             : msgPopup.level === "launch-warning" ? "Play anyway"
                             : msgPopup.level === "setup" || msgPopup.level === "choice"
                               || msgPopup.level === "remove" ? backend.messageAccept : "OK"
                    }
                    ButtonHint {
                        btn: "b"
                        label: "Back"
                        visible: msgPopup.question
                    }
                }
            }
        }
    }
}
