# dialogs.sh — sourced by Janus's shell scripts to ask and tell the user
# things through dialog boxes when there's a desktop session: kdialog, else
# zenity, else PySide6 (installed for Janus anyway); the terminal otherwise.
#
#   TITLE="..."            # optional, before sourcing (default: Janus Split Screen)
#   . scripts/dialogs.sh
#   ask "Text" [OK_LABEL [CANCEL_LABEL [warning]]] && ...   # 0 = OK
#   say info|warning|error "Text"
#   busy_start "Text"; ...; busy_stop
#
# $UI says which one was picked (kdialog, zenity, qt, terminal).

TITLE="${TITLE:-Janus Split Screen}"
UI=terminal
if [ -n "${WAYLAND_DISPLAY:-}${DISPLAY:-}" ]; then
    if command -v kdialog >/dev/null 2>&1; then
        UI=kdialog
    elif command -v zenity >/dev/null 2>&1; then
        UI=zenity
    elif python3 -c 'import PySide6.QtWidgets' >/dev/null 2>&1; then
        UI=qt
    fi
fi

qt_dialog() {  # qt_dialog ask|warn|info|warning|error TEXT [OK_LABEL CANCEL_LABEL] -> 0 = OK
    python3 - "$1" "$2" "$TITLE" "${3:-OK}" "${4:-Cancel}" <<'EOF'
import sys
from PySide6.QtWidgets import QApplication, QMessageBox
kind, text, title, ok, cancel = sys.argv[1:6]
app = QApplication([])
icon = {'ask': QMessageBox.Icon.Question, 'warn': QMessageBox.Icon.Warning,
        'warning': QMessageBox.Icon.Warning, 'error': QMessageBox.Icon.Critical}.get(kind, QMessageBox.Icon.Information)
box = QMessageBox(icon, title, text)
if kind in ('ask', 'warn'):
    yes = box.addButton(ok, QMessageBox.ButtonRole.AcceptRole)
    no = box.addButton(cancel, QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(no if kind == 'warn' else yes)
    box.exec()
    sys.exit(0 if box.clickedButton() is yes else 1)
box.exec()
EOF
}

# ask TEXT [OK_LABEL [CANCEL_LABEL [warning]]] -> 0 = OK, 1 = Cancel.
# "warning" shows a warning and makes Cancel the default.
ask() {
    local text="$1" ok="${2:-OK}" cancel="${3:-Cancel}" warn="${4:-}" reply
    case "$UI" in
        kdialog)
            if [ -n "$warn" ]; then
                kdialog --title "$TITLE" --yes-label "$ok" --no-label "$cancel" --warningyesno "$text"
            else
                kdialog --title "$TITLE" --yes-label "$ok" --no-label "$cancel" --yesno "$text"
            fi ;;
        zenity)
            zenity --question --title "$TITLE" --text "$text" \
                --ok-label "$ok" --cancel-label "$cancel" ${warn:+--default-cancel} ;;
        qt)
            qt_dialog "$([ -n "$warn" ] && echo warn || echo ask)" "$text" "$ok" "$cancel" ;;
        *)
            [ -t 0 ] || return 1
            if [ -n "$warn" ]; then
                read -r -p "$text [y/N] " reply
                case "${reply,,}" in y|yes) return 0 ;; *) return 1 ;; esac
            fi
            read -r -p "$text [Y/n] " reply
            case "${reply,,}" in n|no) return 1 ;; esac
            ;;
    esac
}

# say info|warning|error TEXT
say() {
    local k z
    case "$1" in
        error)   k=--error;  z=--error ;;
        warning) k=--sorry;  z=--warning ;;
        *)       k=--msgbox; z=--info ;;
    esac
    case "$UI" in
        kdialog) kdialog --title "$TITLE" "$k" "$2" ;;
        zenity)  zenity "$z" --title "$TITLE" --text "$2" ;;
        qt)      qt_dialog "$1" "$2" ;;
        *)       if [ "$1" = error ]; then echo "$2" >&2; else echo "$2"; fi ;;
    esac || true
}

# busy_start TEXT / busy_stop: a "please wait" window while something slow runs
# (nothing on the terminal or the PySide6 fallback).
_BUSY=""
busy_start() {
    case "$UI" in
        kdialog) _BUSY="$(kdialog --title "$TITLE" --progressbar "$1" 0 2>/dev/null)" || _BUSY="" ;;
        zenity)  zenity --progress --pulsate --no-cancel --auto-close --title "$TITLE" --text "$1" \
                     < <(sleep 3600) >/dev/null 2>&1 &
                 _BUSY=$! ;;
    esac
}
busy_stop() {
    [ -n "$_BUSY" ] || return 0
    case "$UI" in
        kdialog) local q
                 q="$(command -v qdbus6 || command -v qdbus || true)"
                 [ -n "$q" ] && "$q" $_BUSY close >/dev/null 2>&1 ;;
        zenity)  kill "$_BUSY" 2>/dev/null ;;
    esac
    _BUSY=""
    return 0
}
