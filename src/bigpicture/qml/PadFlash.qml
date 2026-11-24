import QtQuick

// A short glow over a pad's pill or its player's tile whenever that pad
// presses a button, so everyone sees which controller is theirs. Put it in
// the Rectangle to light up; `js` is the pad's kernel js index (-1 = none).
Rectangle {
    id: flash
    property int js: -1
    anchors.fill: parent
    radius: parent.radius
    color: "white"
    opacity: 0

    Connections {
        target: gamepad
        function onButton(name, padJs) {
            if (flash.js >= 0 && padJs === flash.js) pulse.restart()
        }
    }
    SequentialAnimation {
        id: pulse
        NumberAnimation { target: flash; property: "opacity"; to: 0.35; duration: 60 }
        NumberAnimation { target: flash; property: "opacity"; to: 0; duration: 400; easing.type: Easing.OutQuad }
    }
}
