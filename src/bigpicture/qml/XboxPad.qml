import QtQuick
import QtQuick.Shapes

// An Xbox controller drawing for the controller setup wizard (SetupPanel.qml).
// target: the SDL key asked for now (pulses); mapped: keys recorded so far;
// live: key -> value (0..1, stick axes -1..1) read through the new mapping, so
// a recorded control lights up while it's pressed. Drawn in a 600x420 box,
// scaled to the item's width. Back to front: triggers, bumpers, body, controls.
Item {
    id: root
    property string target: ""
    property var mapped: []
    property var live: ({})

    implicitWidth: Theme.px(600)
    implicitHeight: width * 420 / 600

    function val(k) { return root.live[k] || 0 }
    function isMapped(k) { return root.mapped.indexOf(k) >= 0 }
    function isTarget(k) { return root.target === k }

    readonly property color shell: "#222834"      // the body
    readonly property color shellEdge: "#3f4a5d"
    readonly property color well: "#161a21"       // recesses (stick wells, D-pad)
    readonly property color base: "#2d3541"       // a control not recorded yet
    readonly property color set: "#4a576b"        // a recorded control
    readonly property color edge: "#5d6b84"
    readonly property color ask: Qt.rgba(1, 0.71, 0.28, root.pulse)

    function fill(k) { return root.val(k) > 0.5 ? Theme.accent : root.isMapped(k) ? root.set : root.base }

    // Pulse for the control asked for.
    property real pulse: 0
    SequentialAnimation on pulse {
        loops: Animation.Infinite
        running: root.visible && root.target !== ""
        NumberAnimation { from: 0.25; to: 1; duration: 550; easing.type: Easing.InOutSine }
        NumberAnimation { from: 1; to: 0.25; duration: 550; easing.type: Easing.InOutSine }
    }

    // One round/rectangular control: fill = lit / recorded / not yet; border
    // pulses when asked for.
    component Ctl: Rectangle {
        id: c
        property string key
        property color litColor: Theme.accent
        property real level: root.val(key)
        property bool asked: root.isTarget(key)
        color: level > 0.5 ? litColor : root.isMapped(key) ? root.set : root.base
        border.width: asked ? 4 : 2
        border.color: asked ? root.ask : root.edge
        Behavior on color { ColorAnimation { duration: 60 } }
    }

    // Trigger + bumper of one side, drawn for the left and mirrored for the right.
    component Shoulder: Item {
        id: sh
        property bool flip: false
        property string trigger
        property string bumper
        width: 600; height: 420
        transform: Scale { origin.x: 300; xScale: sh.flip ? -1 : 1 }

        // Trigger: only its top shows above the bumper; the fill rises with the pull.
        Rectangle {
            id: trig
            x: 116; y: 2; width: 92; height: 80; radius: 24
            color: root.isMapped(sh.trigger) ? root.set : root.base
            border.width: root.isTarget(sh.trigger) ? 4 : 2
            border.color: root.isTarget(sh.trigger) ? root.ask : root.edge
            Item {
                // Visible part of the trigger is about y 0..50 of the rectangle.
                readonly property real level: Math.max(0, Math.min(1, root.val(sh.trigger)))
                x: 0; width: parent.width
                y: 50 * (1 - level); height: parent.height - y
                clip: true
                visible: level > 0.02
                Rectangle {
                    x: 3; y: 3 - parent.y; width: trig.width - 6; height: trig.height - 6
                    radius: trig.radius - 3
                    color: Theme.accent
                }
            }
        }
        // Bumper: a band along the shoulder, tapering into the body.
        Shape {
            preferredRendererType: Shape.CurveRenderer
            ShapePath {
                fillColor: root.fill(sh.bumper)
                strokeColor: root.isTarget(sh.bumper) ? root.ask : root.edge
                strokeWidth: root.isTarget(sh.bumper) ? 4 : 2
                joinStyle: ShapePath.RoundJoin
                PathSvg { path: "M 252,80 L 252,43 Q 252,33 242,33 C 195,33 152,41 122,61 C 90,82 66,116 54,170 L 120,130 Z" }
            }
        }
    }

    Item {
        id: box
        width: 600
        height: 420
        scale: root.width / 600
        transformOrigin: Item.TopLeft

        Shoulder { trigger: "lefttrigger"; bumper: "leftshoulder" }
        Shoulder { flip: true; trigger: "righttrigger"; bumper: "rightshoulder" }

        // Body
        Shape {
            preferredRendererType: Shape.CurveRenderer
            ShapePath {
                fillColor: root.shell
                strokeColor: root.shellEdge
                strokeWidth: 3
                joinStyle: ShapePath.RoundJoin
                PathSvg { path: "M 300,60 C 350,60 390,62 425,70 C 480,80 520,110 540,170"
                              + " C 560,230 580,300 578,350 C 577,395 545,412 515,400"
                              + " C 480,385 455,335 420,315 C 400,305 380,305 350,305 L 250,305"
                              + " C 220,305 200,305 180,315 C 145,335 120,385 85,400"
                              + " C 55,412 23,395 22,350 C 20,300 40,230 60,170"
                              + " C 80,110 120,80 175,70 C 210,62 250,60 300,60 Z" }
            }
        }

        // Trigger / bumper names, over the shapes (not mirrored).
        component Tag: Text {
            property real cx
            property real cy
            x: cx - width / 2; y: cy - height / 2
            color: Theme.text; font.bold: true
        }
        Tag { cx: 164; cy: 21; text: "LT"; font.pixelSize: 17 }
        Tag { cx: 436; cy: 21; text: "RT"; font.pixelSize: 17 }
        Tag { cx: 216; cy: 48; text: "LB"; font.pixelSize: 14 }
        Tag { cx: 384; cy: 48; text: "RB"; font.pixelSize: 14 }

        // Sticks: the cap moves with the axes and lights when pushed far; a
        // click (L3/R3) lights the well. An asked axis shows its direction on the cap.
        component Stick: Item {
            id: s
            property string click
            property string xk
            property string yk
            property real cx
            property real cy
            x: cx - 39; y: cy - 39; width: 78; height: 78
            readonly property bool askedAxis: root.isTarget(xk) || root.isTarget(yk)
            readonly property bool asked: askedAxis || root.isTarget(click)
            Rectangle {
                anchors.fill: parent; radius: width / 2
                color: root.val(s.click) > 0.5 ? Theme.accent : root.well
                border.width: s.asked ? 4 : 2
                border.color: s.asked ? root.ask : root.isMapped(s.click) ? root.edge : root.shellEdge
                Behavior on color { ColorAnimation { duration: 60 } }
            }
            Rectangle {
                id: cap
                width: 52; height: 52; radius: 26
                x: (s.width - width) / 2 + 11 * root.val(s.xk)
                y: (s.height - height) / 2 + 11 * root.val(s.yk)
                readonly property bool moved: Math.abs(root.val(s.xk)) > 0.5 || Math.abs(root.val(s.yk)) > 0.5
                color: moved ? Theme.accent
                     : (root.isMapped(s.xk) || root.isMapped(s.yk)) ? root.set : root.base
                border.width: 2
                border.color: root.val(s.click) > 0.5 ? Theme.text : root.edge
                Behavior on color { ColorAnimation { duration: 60 } }
                // Inner ring, like the textured rim of a real cap.
                Rectangle {
                    anchors.centerIn: parent; width: 34; height: 34; radius: 17
                    color: "transparent"; border.width: 2
                    border.color: Qt.rgba(1, 1, 1, 0.10)
                    visible: !s.askedAxis
                }
                // Which way to push, while a stick axis is asked for.
                Shape {
                    visible: s.askedAxis
                    anchors.centerIn: parent
                    width: 36; height: 36
                    opacity: 0.35 + 0.65 * root.pulse
                    rotation: root.isTarget(s.yk) ? 90 : 0
                    preferredRendererType: Shape.CurveRenderer
                    ShapePath {
                        fillColor: Theme.warning
                        strokeColor: "transparent"
                        PathSvg { path: "M 3,14 L 19,14 L 19,6 L 34,18 L 19,30 L 19,22 L 3,22 Z" }
                    }
                }
            }
        }
        Stick { cx: 172; cy: 152; click: "leftstick"; xk: "leftx"; yk: "lefty" }
        Stick { cx: 372; cy: 240; click: "rightstick"; xk: "rightx"; yk: "righty" }

        // Face buttons, in their Xbox colours when lit.
        Repeater {
            model: [{ k: "y", x: 428, y: 114, c: "#f5c518" }, { k: "x", x: 390, y: 152, c: "#3b82f6" },
                    { k: "b", x: 466, y: 152, c: "#ef4444" }, { k: "a", x: 428, y: 190, c: "#22c55e" }]
            delegate: Ctl {
                required property var modelData
                key: modelData.k
                litColor: modelData.c
                x: modelData.x - 20; y: modelData.y - 20; width: 40; height: 40; radius: 20
                Text { anchors.centerIn: parent; text: modelData.k.toUpperCase()
                       color: parent.level > 0.5 ? "#101010" : modelData.c
                       font.pixelSize: 19; font.bold: true }
            }
        }

        // D-pad: one cross, its four arms light separately.
        Item {
            id: dpad
            x: 228 - 42; y: 240 - 42; width: 84; height: 84
            Shape {
                preferredRendererType: Shape.CurveRenderer
                ShapePath {
                    fillColor: root.well
                    strokeColor: root.shellEdge
                    strokeWidth: 2
                    joinStyle: ShapePath.RoundJoin
                    PathSvg { path: "M 32,0 L 52,0 Q 57,0 57,5 L 57,27 L 79,27 Q 84,27 84,32 L 84,52 Q 84,57 79,57"
                                  + " L 57,57 L 57,79 Q 57,84 52,84 L 32,84 Q 27,84 27,79 L 27,57 L 5,57"
                                  + " Q 0,57 0,52 L 0,32 Q 0,27 5,27 L 27,27 L 27,5 Q 27,0 32,0 Z" }
                }
            }
            component Arm: Ctl { radius: 5; border.width: asked ? 4 : 0 }
            Arm { key: "dpup";    x: 31; y: 4;  width: 22; height: 26 }
            Arm { key: "dpdown";  x: 31; y: 54; width: 22; height: 26 }
            Arm { key: "dpleft";  x: 4;  y: 31; width: 26; height: 22 }
            Arm { key: "dpright"; x: 54; y: 31; width: 26; height: 22 }
        }

        // Guide, View (back), Menu (start)
        Ctl { key: "guide"; x: 300 - 19; y: 104 - 19; width: 38; height: 38; radius: 19
              Rectangle { anchors.centerIn: parent; width: 16; height: 16; radius: 8
                          color: "transparent"; border.width: 2
                          border.color: parent.level > 0.5 ? "#101010" : Theme.dim } }
        Ctl { key: "back"; x: 262 - 13; y: 152 - 13; width: 26; height: 26; radius: 13
              Rectangle { anchors.centerIn: parent; width: 10; height: 8; radius: 2
                          color: "transparent"; border.width: 1.5
                          border.color: parent.level > 0.5 ? "#101010" : Theme.dim } }
        Ctl { key: "start"; x: 338 - 13; y: 152 - 13; width: 26; height: 26; radius: 13
              Column { anchors.centerIn: parent; spacing: 2
                       Repeater { model: 3
                                  Rectangle { width: 10; height: 1.5
                                              color: parent.parent.level > 0.5 ? "#101010" : Theme.dim } } } }
    }
}
