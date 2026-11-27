"""Compact Model Pack host for Fast Draft's two-gesture surface tool."""
from pathlib import Path
import weakref
import time

from maya import cmds
from maya.api import OpenMaya as om
from maya.api import OpenMayaUI as omui
from PySide6 import QtCore, QtGui, QtWidgets
from shiboken6 import isValid, wrapInstance

from model_pack_draft import tool, patterns
from model_pack_draft.preview import TURQUOISE


ROOT = Path(__file__).resolve().parent
_HOST = None
MANIPULATORS = {QtCore.Qt.Key_Q:'selectSuperContext',QtCore.Qt.Key_W:'moveSuperContext',
                QtCore.Qt.Key_E:'RotateSuperContext',QtCore.Qt.Key_R:'scaleSuperContext'}
SNAP_KEYS = {QtCore.Qt.Key_V:'point',QtCore.Qt.Key_C:'curve',QtCore.Qt.Key_X:'grid'}


def highlight_color():
    """Display sRGB of Maya's blue/teal active toolbar buttons."""
    return TURQUOISE


def active():
    return tool._active is not None and cmds.currentCtx() == tool.CONTEXT


def shutdown(preserve=True):
    global _HOST
    host, _HOST = _HOST, None
    if host is not None and isValid(host):host.stop(preserve=preserve)
    else:tool.stop(preserve=preserve)


def toggle(window, pattern=None):
    global _HOST
    if pattern is None and _HOST is not None and isValid(_HOST) and _HOST.window() is window and active():
        shutdown()
        return
    shutdown()
    window._prepare_tool('fast_draft')
    host = Host(window)
    _HOST = host
    try:
        host.start(pattern=pattern)
    except Exception as error:
        shutdown()
        cmds.warning('Fast Draft: ' + str(error))


def sync(window):
    if getattr(window, '_active_tool', None) == 'fast_draft' and not active():shutdown()
    button = getattr(window, 'fast_draft_button', None)
    if button is not None:button.setChecked(active())


def choose_pattern(window,uuid):
    if not uuid:return
    window._draft_pattern_choice=uuid
    patterns.choose(uuid)
    if _HOST is not None and isValid(_HOST) and _HOST.window() is window:
        try:
            _HOST.pending=0;_HOST.remainder=0.
            _HOST.start_tool(pattern=uuid)
            window._draft_pattern_choice=None
            _HOST.refresh_patterns()
        except (ValueError,RuntimeError) as error:cmds.warning('Fast Draft: '+str(error))


def pattern_menu(window):
    menu=QtWidgets.QMenu(window)
    values=patterns.all_curves()
    try:chosen=patterns.current()
    except ValueError:chosen=None
    saved=menu.addMenu('Scene Curves')
    saved.setEnabled(bool(values))
    for uuid,name in values:
        action=saved.addAction(name.rsplit('|',1)[-1]);action.setCheckable(True);action.setChecked(uuid==chosen)
        action.triggered.connect(lambda checked=False,uuid=uuid:choose_pattern(window,uuid))
    return menu


class Host(QtCore.QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window_ref = weakref.ref(window)
        self.panel = None
        self.stopped = False
        self.switching = False
        self.pending = 0
        self.remainder = 0.0
        self.scheduled = False
        self.callbacks = []
        self.undo_group=None
        self.hover_signature=None;self.hover_refresh=0.
        self.held_snap_keys=[]
        self.hover_timer=QtCore.QTimer(self)
        self.hover_timer.setInterval(60);self.hover_timer.timeout.connect(self.update_hover)
        self.snap_enabled=bool(self.option('ModelPack_DraftSnapCenters',1))
        self.compact_size = QtCore.QSize(window.size())
        self.shape = self.option('ModelPack_DraftShape', 'cylinder')
        if self.shape not in ('box', 'cylinder', 'line'):self.shape = 'cylinder'
        self.counts = {
            'cylinder': max(3, min(256, int(self.option('ModelPack_DraftSides', 32)))),
        }

    def window(self):
        window = self.window_ref()
        return window if window is not None and isValid(window) else None

    @staticmethod
    def option(name, default):
        return cmds.optionVar(q=name) if cmds.optionVar(exists=name) else default

    def start(self,pattern=None):
        window = self.window()
        pattern=pattern or getattr(window,'_draft_pattern_choice',None) or patterns.selected()
        from model_pack_theme import UndoGroup
        self.undo_group=UndoGroup('Fast Draft')
        window._draft_pattern_choice=None
        self.panel = QtWidgets.QWidget(window)
        self.panel.setObjectName('modelPackFastDraftSettings')
        layout = QtWidgets.QVBoxLayout(self.panel)
        layout.setContentsMargins(0, 5, 0, 0);layout.setSpacing(6)
        self.choice = QtWidgets.QComboBox(self.panel)
        self.choice.addItem(QtGui.QIcon(str(ROOT / 'icons' / 'draft_cylinder.svg')), 'Cylinder', 'cylinder')
        self.choice.addItem(QtGui.QIcon(str(ROOT / 'icons' / 'draft_cube.svg')), 'Cube', 'box')
        self.choice.addItem('Line Sketch', 'line')
        self.choice.setItemData(1, 'Shift: ровный квадрат. Ctrl: прямоугольник от центра. Shift + Ctrl: квадрат от центра.', QtCore.Qt.ToolTipRole)
        self.choice.setCurrentIndex(self.choice.findData(self.shape))
        self.choice.setMinimumHeight(34)
        self.choice.setToolTip('Форма примитива. Готовое основание сохраняется при смене формы.')
        self.choice.currentIndexChanged.connect(self.change_shape)
        layout.addWidget(self.choice)
        self.snap_check=QtWidgets.QCheckBox('Snap to Centers',self.panel)
        self.snap_check.setChecked(self.snap_enabled)
        self.snap_check.setToolTip('Начальная точка: середина ребра или центр полигона под курсором. '
                                  'Подведите курсор к бирюзовому маркеру и начните рисование.')
        self.snap_check.toggled.connect(self.change_snapping)
        layout.addWidget(self.snap_check)
        self.line_settings=QtWidgets.QWidget(self.panel)
        line_layout=QtWidgets.QHBoxLayout(self.line_settings);line_layout.setContentsMargins(0,0,0,0)
        self.line_parallel=QtWidgets.QCheckBox('Parallel / Perpendicular',self.line_settings)
        self.line_parallel.setChecked(True)
        self.line_angle=QtWidgets.QSpinBox(self.line_settings);self.line_angle.setRange(0,90)
        self.line_angle.setValue(15);self.line_angle.setSuffix('°')
        self.line_angle.setToolTip('Шаг привязки угла; 0 отключает привязку. Допуск захвата — 4°.')
        line_layout.addWidget(self.line_parallel);line_layout.addWidget(self.line_angle)
        layout.addWidget(self.line_settings)
        self.line_parallel.toggled.connect(self.change_line_settings)
        self.line_angle.valueChanged.connect(self.change_line_settings)
        self.finish_line=QtWidgets.QPushButton('Finish Sketch · Enter',self.panel)
        self.finish_line.clicked.connect(lambda:self.finish_sketch())
        layout.addWidget(self.finish_line)
        self.hint = QtWidgets.QLabel(self.panel)
        self.hint.setAlignment(QtCore.Qt.AlignCenter)
        self.hint.setStyleSheet('color:#a4adb7;font-size:10px;')
        layout.addWidget(self.hint)
        self.saved=QtWidgets.QComboBox(self.panel)
        self.saved.setPlaceholderText('Scene Curves')
        self.saved.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.saved.setMinimumContentsLength(12)
        self.saved.setToolTip('Выберите кривую для выдавливания её текущего контура. '
                             'Открытая кривая — стенка; замкнутая — объём. Для вычитания выделите кривую и меш '
                             'или начните жест глубины на нужном меше. Точки редактируются в Maya.')
        self.saved.activated.connect(lambda index:choose_pattern(window,self.saved.itemData(index)))
        layout.addWidget(self.saved)
        self.clear_sketches=QtWidgets.QPushButton('Clear Sketches',self.panel)
        self.clear_sketches.setToolTip('Удалить кривые и эскизы Fast Draft. Ctrl+Z восстанавливает их.')
        layout.addWidget(self.clear_sketches)
        self.clear_sketches.clicked.connect(self.clear_saved_sketches)
        self.panel.setToolTip('ЛКМ: нарисовать основание на меше, затем потянуть от поверхности — добавить, внутрь — вырезать. '
                              'Куб: Shift — ровный квадрат; Ctrl — прямоугольник от центра; Shift + Ctrl — квадрат от центра. '
                              'Shift + колесо: стороны цилиндра. T на созданном цилиндре: крышка. '
                              'Удерживайте V: вершины; C: кривые; X: сетка Maya. Отпустите клавишу — свободное рисование. '
                              'Esc: отменить рисунок. Q / W / E / R: сохранить основание и переключить инструмент. '
                              'Выделите любую кривую Maya или Pencil Curve и откройте Fast Draft для выдавливания. '
                              'Открытая кривая создаёт стенку, замкнутая — объём для вычитания. Список — Scene Curves.')
        window.layout().addWidget(self.panel, window._panel_row, 0, 1, 2)
        self.panel.show()
        self.start_tool(pattern=pattern)
        self.refresh_patterns()
        self.hover_timer.start()
        QtWidgets.QApplication.instance().installEventFilter(self)
        self.callbacks = [om.MSceneMessage.addCallback(kind, lambda *_: shutdown(preserve=False))
                          for kind in (om.MSceneMessage.kBeforeNew, om.MSceneMessage.kBeforeOpen)]
        self.callbacks.append(om.MEventMessage.addEventCallback('ToolChanged',self.tool_changed))
        self.callbacks.append(om.MEventMessage.addEventCallback('SelectionChanged',self.refresh_patterns))
        window.fast_draft_button.setChecked(True)
        window.layout().activate();window.resize(window.width(), window.sizeHint().height())

    def refresh_patterns(self,*_):
        if self.stopped:return
        try:chosen=(getattr(self.window(),'_draft_pattern_choice',None) or
                    getattr(tool._active,'pattern_uuid',None) or patterns.current())
        except ValueError:chosen=None
        self.saved.blockSignals(True);self.saved.clear()
        for uuid,name in patterns.all_curves():self.saved.addItem(name.rsplit('|',1)[-1],uuid)
        self.saved.setCurrentIndex(self.saved.findData(chosen));self.saved.blockSignals(False)

    def start_tool(self,pattern=None):
        self.switching=False
        self.clear_snap_keys()
        target=patterns.selected_target() if pattern else None
        engine = tool.start(shape=self.shape, segments=self.counts['cylinder'])
        engine.preview.color = highlight_color()
        engine.set_snapping(self.snap_enabled)
        self.hover_signature=None;self.hover_refresh=0.
        if self.shape == 'cylinder':engine.set_sections(self.counts['cylinder'])
        if pattern:
            engine.resume(pattern,target=target);self.shape=engine.shape
            if self.shape=='cylinder':self.counts['cylinder']=engine.segments
            self.choice.blockSignals(True);self.choice.setCurrentIndex(self.choice.findData(self.shape));self.choice.blockSignals(False)
        self.line_settings.setVisible(self.shape=='line')
        self.finish_line.setVisible(self.shape=='line')
        self.change_line_settings()
        self.update_hint()

    def clear_saved_sketches(self):
        from model_pack_draft.cleanup import clear_curves
        # An explicit cleanup finishes this drawing session before deleting data.
        engine=tool._active
        engine.cancel(preserve=True)
        try:
            count=clear_curves()
            self.refresh_patterns()
            engine.message('Sketches removed: '+str(count))
        except (RuntimeError,ValueError) as error:cmds.warning('Fast Draft: '+str(error))

    def change_line_settings(self,*_):
        engine=tool._active
        if isinstance(engine,tool.LineSketch):
            engine.angle_step=self.line_angle.value();engine.line_snap=self.line_parallel.isChecked()

    def finish_sketch(self):
        engine=tool._active
        if isinstance(engine,tool.LineSketch):
            try:
                if engine.finish_sketch():self.refresh_patterns()
            except (RuntimeError,ValueError) as error:cmds.warning('Fast Draft: '+str(error))

    def change_snapping(self,enabled):
        self.snap_enabled=bool(enabled)
        cmds.optionVar(intValue=('ModelPack_DraftSnapCenters',int(enabled)))
        self.hover_signature=None
        if active():tool._active.set_snapping(enabled)

    def update_hover(self):
        if self.stopped or self.switching or not active() or cmds.about(batch=True):return
        engine=tool._active
        app=QtWidgets.QApplication.instance()
        snap_mode=engine.snap_mode
        if engine.phase not in ('ready','sketch') or engine.dragging:return
        def clear():
            self.hover_signature=None
            if engine.preview.snap is not None:engine.preview.set_snap(None,engine.view)
        if (not (self.snap_enabled or snap_mode or engine.phase=='sketch') or QtGui.QGuiApplication.applicationState()!=QtCore.Qt.ApplicationActive or
                app.mouseButtons()!=QtCore.Qt.NoButton or app.keyboardModifiers() & QtCore.Qt.AltModifier or
                app.activeModalWidget() is not None or app.activePopupWidget() is not None):
            clear();return
        panel=cmds.getPanel(underPointer=True)
        if not panel or cmds.getPanel(typeOf=panel)!='modelPanel':clear();return
        try:
            view=omui.M3dView.getM3dViewFromModelPanel(panel)
            widget=wrapInstance(int(view.widget()),QtWidgets.QWidget)
            cursor=QtGui.QCursor.pos();local=widget.mapFromGlobal(cursor)
            target=app.widgetAt(cursor)
            if (not widget.isVisible() or not widget.rect().contains(local) or target is None or
                    not (target is widget or widget.isAncestorOf(target))):clear();return
            position=(local.x(),widget.height()-1-local.y())
            # Qt reports logical widget pixels; Maya's rays, projection and
            # draggerContext use viewport pixels, which differ on HiDPI screens.
            try:
                position=(local.x()*view.portWidth()/widget.width(),
                          view.portHeight()-1-local.y()*view.portHeight()/widget.height())
            except AttributeError:pass
            camera=tuple(view.modelViewMatrix())+tuple(view.projectionMatrix())
            signature=(id(engine),panel,position,camera,snap_mode)
            now=time.monotonic();refresh=now-self.hover_refresh>=.5
            if signature==self.hover_signature and not refresh:return
            self.hover_signature=signature
            if refresh:self.hover_refresh=now
            engine.hover(view,panel,position,refresh_meshes=refresh)
        except (RuntimeError,TypeError,ValueError):clear()

    def update_hint(self):
        self.hint.setVisible(self.shape == 'cylinder' and not getattr(getattr(tool._active,'footprint',None),'from_curve',False))
        self.hint.setText('Shift + Wheel · сторон: {}'.format(self.counts['cylinder']))
        window = self.window()
        if window is not None:
            window.layout().activate();window.resize(window.width(), window.sizeHint().height())

    def change_shape(self, *_):
        if self.stopped:return
        self.pending = 0;self.remainder = 0.0
        self.shape = self.choice.currentData()
        cmds.optionVar(stringValue=('ModelPack_DraftShape', self.shape))
        self.start_tool()

    def owns_input(self, target):
        if not isinstance(target, QtWidgets.QWidget):return False
        window = self.window()
        if window is not None and (target is window or window.isAncestorOf(target)):return True
        for panel in cmds.getPanel(type='modelPanel') or []:
            try:
                view = omui.M3dView.getM3dViewFromModelPanel(panel)
                widget = wrapInstance(int(view.widget()), QtWidgets.QWidget)
                if widget is not None and (target is widget or widget.isAncestorOf(target)):return True
            except RuntimeError:
                continue
        return False

    @staticmethod
    def text_input(target):
        return (isinstance(target,(QtWidgets.QLineEdit,QtWidgets.QTextEdit,QtWidgets.QPlainTextEdit,QtWidgets.QAbstractSpinBox)) or
                isinstance(target,QtWidgets.QComboBox) and target.isEditable())

    def owns_cancel_input(self,target):
        if self.owns_input(target):return True
        engine=tool._active
        if engine is None or engine.footprint is None or engine.view is None:return False
        try:
            viewport=wrapInstance(int(engine.view.widget()),QtWidgets.QWidget)
            if viewport is None or not isValid(viewport):return False
            window=viewport.window()
            # During a captured Maya drag, keys may target the viewport's
            # top-level window rather than the QWidget below the pointer.
            return (target is window or target is window.windowHandle() or
                    isinstance(target,QtWidgets.QWidget) and target.window() is window)
        except (AttributeError,RuntimeError,TypeError):return False

    def tool_changed(self,*_):
        # A shape change briefly switches through Select. Inspect the final
        # context after the start/stop operation has completed.
        QtCore.QTimer.singleShot(0,self.check_context)

    def check_context(self):
        timer = getattr(self.window(), '_scene_reset_timer', None)
        if not self.stopped and _HOST is self and not active() and (timer is None or not timer.isActive()):
            shutdown()

    def switch_manipulator(self,key,engine):
        if self.stopped or _HOST is not self or tool._active is not engine:return
        if not active():
            shutdown();return  # A later tool choice takes precedence.
        shutdown()
        context=MANIPULATORS[key]
        if cmds.currentCtx()!=context:cmds.setToolTo(context)

    def clear_snap_keys(self):
        self.held_snap_keys.clear()
        self.sync_snap_keys()

    def sync_snap_keys(self):
        self.hover_signature=None
        engine=tool._active
        if engine is None:return
        engine.snap_mode=SNAP_KEYS[self.held_snap_keys[-1]] if self.held_snap_keys else None
        # Remove the old marker immediately; the next hover/drag will draw the
        # currently held mode. The footprint's chosen origin never moves.
        engine.preview.set_snap(None,engine.view)

    def snap_key_event(self,target,event,editing):
        key=event.key();kind=event.type()
        if key not in SNAP_KEYS:return False
        held=key in self.held_snap_keys
        release=kind==QtCore.QEvent.KeyRelease
        # A key pressed before Fast Draft (or in a text input) belongs to Maya.
        # An owned release must still work outside the viewport or with Ctrl.
        if release and not held:return False
        if not held:
            if self.switching or editing or not self.owns_cancel_input(target):return False
            modifiers=event.modifiers()
            if modifiers&(QtCore.Qt.AltModifier|QtCore.Qt.MetaModifier):return False
            if modifiers&QtCore.Qt.ControlModifier and tool._active.phase!='base_drag':return False
        if kind!=QtCore.QEvent.ShortcutOverride and not event.isAutoRepeat():
            if release:self.held_snap_keys.remove(key)
            elif not held:self.held_snap_keys.append(key)
            self.sync_snap_keys()
            self.refresh_base(tool._active)
        # Claim the shortcut as well as the key, so Maya's native press command
        # cannot latch a global snap flag behind the tool's momentary state.
        event.accept();return True

    def eventFilter(self, target, event):
        if self.stopped or not active():return False
        kind = event.type()
        if kind==QtCore.QEvent.ApplicationDeactivate:
            self.clear_snap_keys()
            self.refresh_base(tool._active)
            return False
        if kind not in (QtCore.QEvent.Wheel, QtCore.QEvent.KeyPress, QtCore.QEvent.KeyRelease,QtCore.QEvent.ShortcutOverride):return False
        if kind!=QtCore.QEvent.Wheel:
            modifiers=event.modifiers()
            escape=event.key()==QtCore.Qt.Key_Escape and not modifiers&(QtCore.Qt.AltModifier|QtCore.Qt.MetaModifier)
            switch=event.key() in MANIPULATORS and modifiers==QtCore.Qt.NoModifier
            app=QtWidgets.QApplication.instance()
            editing=self.text_input(target)
            if not editing and self.text_input(app.focusWidget()) and not tool._active.dragging:
                editing=not self.owns_input(target)
            if self.snap_key_event(target,event,editing):return True
            redo=event.matches(QtGui.QKeySequence.Redo)
            undo=event.matches(QtGui.QKeySequence.Undo)
            if event.key()==QtCore.Qt.Key_Z and not modifiers&(QtCore.Qt.ControlModifier|QtCore.Qt.AltModifier|QtCore.Qt.MetaModifier):
                redo=bool(modifiers&QtCore.Qt.ShiftModifier);undo=not redo
            if (undo or redo) and not editing and self.owns_cancel_input(target):
                if kind==QtCore.QEvent.KeyPress and not event.isAutoRepeat():
                    # Discard the transient preview, then use native scene Undo.
                    # Never save a new curve while handling an Undo keystroke.
                    tool._active.cancel(preserve=False)
                    cmds.redo() if redo else cmds.undo()
                    self.refresh_patterns()
                event.accept();return True
            if (event.key() in (QtCore.Qt.Key_Return,QtCore.Qt.Key_Enter) and not editing and
                    self.owns_cancel_input(target) and isinstance(tool._active,tool.LineSketch) and tool._active.phase=='sketch'):
                if kind==QtCore.QEvent.KeyPress and not event.isAutoRepeat():self.finish_sketch()
                event.accept();return True
            if (escape or switch) and not editing and self.owns_cancel_input(target):
                if kind==QtCore.QEvent.KeyPress and not event.isAutoRepeat() and not self.switching:
                    engine=tool._active
                    # Cancel now: a mouse release can arrive before the next
                    # event-loop turn and must never commit this footprint.
                    self.cancel_drawing(preserve=switch)
                    if switch:
                        self.switching=True;key=event.key()
                        QtCore.QTimer.singleShot(0,lambda:self.switch_manipulator(key,engine))
                event.accept();return True
            if kind==QtCore.QEvent.ShortcutOverride:return False
        if not self.owns_input(target):return False
        if kind == QtCore.QEvent.Wheel:
            if getattr(tool._active.footprint,'from_curve',False):return False
            if self.shape != 'cylinder':return False
            modifiers = event.modifiers()
            if not modifiers & QtCore.Qt.ShiftModifier or modifiers & (QtCore.Qt.ControlModifier | QtCore.Qt.AltModifier):
                return False
            amount = event.angleDelta().y() / 120.0
            if not amount:amount = event.pixelDelta().y() / 40.0
            self.remainder += amount
            steps = int(self.remainder)
            self.remainder -= steps
            self.pending += steps
            self.queue_wheel()
            event.accept()
            return True
        if event.key() in (QtCore.Qt.Key_Shift, QtCore.Qt.Key_Control) and self.shape == 'box' and not event.isAutoRepeat():
            engine = tool._active
            QtCore.QTimer.singleShot(0, lambda: self.refresh_base(engine))
        return False

    def refresh_base(self, engine):
        # Refresh a held gesture even if Shift/Ctrl changes with a still mouse.
        # A queued key event must not touch a new tool or the depth gesture.
        if self.stopped or self.switching or not active() or tool._active is not engine:return
        if engine.phase == 'base_drag' and engine.dragging:engine.drag()

    def queue_wheel(self):
        if self.scheduled:return
        self.scheduled = True
        QtCore.QTimer.singleShot(0, self.apply_wheel)

    def apply_wheel(self):
        self.scheduled = False
        delta, self.pending = self.pending, 0
        if not delta or self.stopped or self.switching or not active() or self.shape != 'cylinder':return
        if getattr(tool._active.footprint,'from_curve',False):return
        count = max(3, min(256, self.counts['cylinder'] + delta))
        try:
            tool._active.set_sections(count)
            self.counts['cylinder'] = count
            cmds.optionVar(intValue=('ModelPack_DraftSides', count))
            self.update_hint()
        except Exception as error:
            tool._active.cancel()
            cmds.warning('Fast Draft: ' + str(error))

    def cancel_drawing(self,preserve=False):
        if self.stopped or not active():return
        self.pending = 0;self.remainder = 0.0
        self.clear_snap_keys()
        self.hover_signature=None
        tool._active.cancel(preserve=preserve)
        tool._active.message('Рисунок отменён. Нарисуйте новое основание. Q / W / E / R — сменить инструмент.')
        cmds.refresh(currentView=True, force=True)

    def stop(self,preserve=True):
        if self.stopped:return
        self.clear_snap_keys()
        self.stopped = True;self.pending = 0
        self.hover_timer.stop()
        app = QtWidgets.QApplication.instance()
        if app is not None:app.removeEventFilter(self)
        for callback in self.callbacks:om.MMessage.removeCallback(callback)
        self.callbacks = []
        try:tool.stop(preserve=preserve)
        finally:
            if self.undo_group:self.undo_group.close();self.undo_group=None
        window = self.window()
        if self.panel is not None and isValid(self.panel):
            self.panel.hide()
            if window is not None:window.layout().removeWidget(self.panel)
            self.panel.deleteLater()
        if window is not None:
            if window._active_tool == 'fast_draft':window._active_tool = None
            window.fast_draft_button.setChecked(False)
            window.layout().activate();window.resize(self.compact_size)
        self.deleteLater()
