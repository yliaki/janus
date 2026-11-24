"""Big Picture's "Set up a controller" wizard.

For a pad SDL has no mapping for (Janus input would turn it into an Xbox pad
that does nothing): it asks for each button / stick / trigger in turn, records
what the pad sends (pad_mappings.MappingRecorder on SDL's raw joystick state)
and saves an SDL mapping (~/.config/janus/pads.json), which the input daemon,
the probe and Big Picture itself use from then on.

The pad being set up can't steer the wizard (its presses are the answers):
other pads, the keyboard or the mouse do — X skips a step, Y goes back, B /
Esc cancels. With no other pad the hints turn into clickable buttons with
their keys (``otherPads``).

The pad list says how each pad is mapped (``status``: a Janus mapping made
here, SDL's own, none); X on one with a Janus
mapping removes it (after a confirmation), at once and for every game. A drawing of an Xbox pad (qml/XboxPad.qml) shows the
control asked for and lights up every recorded one while it's pressed
(``live``). After saving, the pad stays live for a test until closed (its own
Start closes too, so a single pad isn't stuck).
"""

import ctypes

from PySide6.QtCore import QObject, QTimer, Signal, Slot, Property

from controllers import list_controllers
import pad_mappings


class SetupWizard(QObject):
    changed = Signal()
    liveChanged = Signal()
    mappingsChanged = Signal()       # a mapping was saved or removed

    def __init__(self, gamepad, parent=None):
        super().__init__(parent)
        self._gamepad = gamepad
        self._active = False
        self._choosing = False
        self._choices: list = []     # [{js, name, guid, status}]
        self._confirm = None         # the choice whose mapping X would remove
        self._confirm_only = False   # asked from the options: close after
        self._info = ''              # shown above the pad list (removed ...)
        self._others = False
        self._current = 0
        self._js = -1
        self._joy = None
        self._rec = None
        self._name = ''
        self._message = ''
        self._live: dict = {}
        self._testing = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)

    # --- QML ------------------------------------------------------------------

    @Property(bool, notify=changed)
    def active(self):
        return self._active

    @Property(bool, notify=changed)
    def choosing(self):
        return self._choosing

    @Property(str, notify=changed)
    def confirming(self):
        """Name of the pad whose Janus mapping is about to be removed."""
        return self._confirm['name'] if self._confirm else ''

    @Property(str, notify=changed)
    def info(self):
        return self._info

    @Property(bool, notify=changed)
    def otherPads(self):
        """Another controller can steer (skip/back/cancel) while recording."""
        return self._others

    @Property('QVariantList', notify=changed)
    def choices(self):
        return list(self._choices)

    @Property(int, notify=changed)
    def current(self):
        return self._current

    @Property(str, notify=changed)
    def padName(self):
        return self._name

    @Property(int, notify=changed)
    def step(self):
        return self._rec.step if self._rec else 0

    @Property(int, notify=changed)
    def steps(self):
        return len(self._rec.steps) if self._rec else 0

    @Property(str, notify=changed)
    def prompt(self):
        return self._rec.prompt if self._rec else ''

    @Property(str, notify=changed)
    def target(self):
        """SDL key of the control asked for now ('a', 'dpup', 'leftx'...)."""
        return self._rec.steps[self._rec.step][0] if self._rec and not self._rec.finished else ''

    @Property('QVariantList', notify=changed)
    def mapped(self):
        return list(self._rec.bindings) if self._rec else []

    @Property(str, notify=changed)
    def note(self):
        """Why the last press didn't count (it's set already)."""
        if self._rec and self._rec.taken and not self._rec.finished:
            return (f'That one is set already as '
                    f'{pad_mappings.LABELS.get(self._rec.taken, self._rec.taken)}. '
                    f'Press another, or go back with Y.')
        return ''

    @Property(bool, notify=changed)
    def testing(self):
        """Saved; the pad stays live so it can be tried out."""
        return self._testing

    @Property('QVariantMap', notify=liveChanged)
    def live(self):
        """Recorded control → its value now (0..1, sticks -1..1)."""
        return dict(self._live)

    @Property(str, notify=changed)
    def message(self):
        """Set when the wizard is over (saved / failed): shown until closed."""
        return self._message

    @Slot()
    def open(self):
        """Pick the pad: the only unrecognised one directly, else a list."""
        if self._gamepad.sdl is None:
            self._finish('SDL is unavailable, so controllers can\'t be set up.')
            return
        self._active, self._message, self._info = True, '', ''
        self._confirm, self._confirm_only = None, False
        self._list_pads()
        if not self._choices:
            self._finish('No controller is connected.')
            return
        unknown = [c for c in self._choices if c['status'] == 'unknown']
        if len(unknown) == 1 and len(self._choices) == 1:
            self._start(unknown[0])
        else:
            self._choosing, self._current = True, 0
            self.changed.emit()

    def _list_pads(self):
        unknown = {p['js'] for p in self._gamepad.unknownPads}
        janus = pad_mappings.janus_mappings()
        out = []
        for c in list_controllers():
            guid = self._gamepad.guid(c.index)
            status = ('janus' if guid in janus else 'unknown' if c.index in unknown
                      else 'known')
            out.append({'js': c.index, 'name': c.name, 'guid': guid, 'status': status})
        order = {'unknown': 0, 'janus': 1, 'known': 2}
        out.sort(key=lambda c: order[c['status']])           # unrecognised first
        self._choices = out
        self._current = min(self._current, max(0, len(out) - 1))

    @Slot(str, int)
    def handle(self, btn, js):
        """Navigation from other pads / keyboard / clicked hints."""
        if self._message:
            if btn in ('a', 'b', 'start', 'x', 'y'):
                self.close()
            return
        if self._confirm is not None:
            if btn == 'a':
                c = self._confirm
                self._gamepad.remove_mapping(c['guid'])
                print(f'[setup] removed mapping of {c["name"]} ({c["guid"]})', flush=True)
                self._confirm = None
                self.mappingsChanged.emit()
                if self._confirm_only:
                    self.close()
                    return
                # The pad list again, with the pad's new state (SDL rescans
                # the pads within a second; the list is rebuilt from pads.json).
                self._list_pads()
                after = next((x['status'] for x in self._choices if x['js'] == c['js']), '')
                self._info = (f'Removed the mapping of {c["name"]}. ' +
                              ('It isn\'t recognised any more: set it up again to use it '
                               'with Janus input.' if after == 'unknown' else
                               'It\'s back to its standard mapping.'))
            elif btn in ('b', 'x'):
                self._confirm = None
                if self._confirm_only:
                    self.close()
                    return
            self.changed.emit()
            return
        if self._choosing:
            if btn == 'up':
                self._current = max(0, self._current - 1)
            elif btn == 'down':
                self._current = min(len(self._choices) - 1, self._current + 1)
            elif btn == 'a':
                self._start(self._choices[self._current])
                return
            elif btn == 'x':
                c = self._choices[self._current] if self._choices else None
                if c and c['status'] == 'janus':
                    self._confirm = c
            elif btn == 'b':
                self.close()
                return
            self.changed.emit()
            return
        if btn == 'x':
            self._rec.skip()
            self._rec.taken = None
            self._after_step()
        elif btn == 'y':
            self._rec.back()
            self._rec.taken = None
            self.changed.emit()
        elif btn == 'b':
            self.close()

    @Slot(str, str)
    def askRemove(self, guid, name):
        """The options' "Remove" on a mapping: confirm, remove, close."""
        self._active, self._choosing, self._confirm_only = True, True, True
        self._message, self._info = '', ''
        self._choices = []
        self._confirm = {'js': -1, 'name': name, 'guid': guid}
        self.changed.emit()

    @Slot(int)
    def select(self, i):
        """The mouse hovers a pad in the list."""
        if self._choosing and self._confirm is None and 0 <= i < len(self._choices) \
                and i != self._current:
            self._current = i
            self.changed.emit()

    @Slot(int)
    def choose(self, i):
        if 0 <= i < len(self._choices):
            self._start(self._choices[i])

    @Slot()
    def close(self):
        self._stop()
        self._active = self._choosing = False
        self._message, self._info, self._confirm = '', '', None
        self._confirm_only = False
        self.changed.emit()

    # --- recording --------------------------------------------------------------

    def _start(self, choice):
        sdl = self._gamepad.sdl
        index = self._gamepad.device_index(choice['js'])
        if index < 0:
            self._finish(f'{choice["name"]} isn\'t there any more.')
            return
        self._joy = sdl.SDL_JoystickOpen(index)
        if not self._joy:
            self._finish(f'Couldn\'t open {choice["name"]}: {sdl.SDL_GetError().decode()}')
            return
        buf = ctypes.create_string_buffer(33)
        sdl.SDL_JoystickGetGUIDString(sdl.SDL_JoystickGetDeviceGUID(index), buf, 33)
        print(f'[setup] {choice["name"]} (js{choice["js"]}): '
              f'{sdl.SDL_JoystickNumButtons(self._joy)} buttons, '
              f'{sdl.SDL_JoystickNumAxes(self._joy)} axes, '
              f'{sdl.SDL_JoystickNumHats(self._joy)} hats', flush=True)
        self._name = choice['name']
        self._js = choice['js']
        self._gamepad.ignore_js.add(self._js)
        self._rec = pad_mappings.MappingRecorder(buf.value.decode(), self._name)
        self._choosing, self._info = False, ''
        self._others = bool(self._gamepad.navigators())
        rest = self._snapshot()
        print(f'[setup] rest state: {rest}', flush=True)
        self._rec.feed(rest)                      # rest state
        self._live, self._testing = {}, False
        self._timer.start(10)
        self.changed.emit()

    def _snapshot(self):
        sdl, j = self._gamepad.sdl, self._joy
        sdl.SDL_JoystickUpdate()
        return ([sdl.SDL_JoystickGetButton(j, i) for i in range(sdl.SDL_JoystickNumButtons(j))],
                [sdl.SDL_JoystickGetAxis(j, i) for i in range(sdl.SDL_JoystickNumAxes(j))],
                [sdl.SDL_JoystickGetHat(j, i) for i in range(sdl.SDL_JoystickNumHats(j))])

    def _poll(self):
        if not self._gamepad.sdl.SDL_JoystickGetAttached(self._joy):
            if self._testing:
                self.close()
            else:
                self._finish(f'{self._name} was disconnected; nothing was saved.')
            return
        others = bool(self._gamepad.navigators())
        if others != self._others:
            self._others = others
            self.changed.emit()
        state = self._snapshot()
        live = self._rec.values(state)
        if live != self._live:
            if self._testing and live.get('start') and not self._live.get('start'):
                self.close()                  # the pad's own Start: done
                return
            self._live = live
            self.liveChanged.emit()
        if self._testing:
            return
        taken = self._rec.taken
        if self._rec.feed(state):
            key = self._rec.steps[self._rec.step - 1][0]
            print(f'[setup] {key} = {self._rec.bindings[key]}', flush=True)
            self._after_step()
        elif self._rec.taken != taken:
            if self._rec.taken:
                print(f'[setup] refused: already {self._rec.taken}', flush=True)
            self.changed.emit()

    def _after_step(self):
        if self._rec.finished:
            if len(self._rec.bindings) < 4:
                self._finish('Too few buttons were set; nothing was saved.')
                return
            mapping = self._rec.mapping()
            print(f'[setup] saved: {mapping}', flush=True)
            pad_mappings.save_mapping(mapping)
            self._gamepad.add_mapping(mapping)
            self.mappingsChanged.emit()
            # Keep polling: the drawing shows the new mapping live.
            self._testing = True
            self._message = (f'{self._name} is set up: {len(self._rec.bindings)} '
                             f'controls. Janus input now makes it an Xbox controller, '
                             f'in every game. Try it out above; press its Start '
                             f'to finish.')
            self.changed.emit()
            return
        self.changed.emit()

    def _stop(self):
        self._timer.stop()
        self._testing = False
        self._live = {}
        if self._joy is not None and self._gamepad.sdl is not None:
            self._gamepad.sdl.SDL_JoystickClose(self._joy)
        self._joy = None
        self._gamepad.ignore_js.discard(self._js)
        self._js = -1

    def _finish(self, message):
        self._stop()
        self._choosing = False
        self._active = True
        self._message = message
        self.changed.emit()
