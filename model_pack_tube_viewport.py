"""Shift + right drag changes Reset Tube sides without replacing Maya tools."""
import math
from PySide6 import QtCore, QtGui, QtWidgets
from shiboken6 import isValid, wrapInstance
from maya import cmds
from maya.api import OpenMaya as om, OpenMayaUI as omui


def viewport_widget(target, point):
    widget_event = isinstance(target, QtWidgets.QWidget)
    native_event = isinstance(target, QtGui.QWindow)
    if not (widget_event or native_event):
        return None
    hit = QtWidgets.QApplication.widgetAt(point)
    for panel in cmds.getPanel(type='modelPanel') or []:
        try:
            view = omui.M3dView.getM3dViewFromModelPanel(panel)
            widget = wrapInstance(int(view.widget()), QtWidgets.QWidget)
            if not widget or not isValid(widget) or not widget.isVisible():
                continue
            if not widget.rect().contains(widget.mapFromGlobal(point)):
                continue
            if widget_event and (target is widget or widget.isAncestorOf(target)):
                return widget

            # The viewport can receive input through a containing widget or a
            # native child window. Check the widget under the event position
            # before accepting events from a shared parent.
            covered = hit is not None and (
                hit is widget or widget.isAncestorOf(hit) or hit.isAncestorOf(widget)
            )
            if widget_event and covered and target.isAncestorOf(widget):
                return widget
            if native_event and (covered or hit is None):
                handles = (widget.windowHandle(), widget.window().windowHandle())
                window = target
                while isinstance(window, QtGui.QWindow):
                    if window in handles:
                        return widget
                    window = window.parent()
        except (RuntimeError, TypeError):
            continue
    return None


class TubeGesture(QtCore.QObject):
    def __init__(self, toolbox):
        super().__init__(toolbox)
        self.toolbox = toolbox; self.drag = None; self.closed = False; self.suppress_menu = False
        self.callback = om.MEventMessage.addEventCallback('ToolChanged', lambda *_: self.finish())
        self.scene_callbacks=[om.MSceneMessage.addCallback(kind,lambda *_:self.toolbox._close_edit_panel())
            for kind in (om.MSceneMessage.kBeforeNew,om.MSceneMessage.kBeforeOpen,om.MSceneMessage.kMayaExiting)]
        QtWidgets.QApplication.instance().installEventFilter(self)

    def begin(self, point, angle=False):
        nodes = [n for n in self.toolbox._modifier_nodes if cmds.objExists(n)]
        if not nodes: return False
        attr='reductionAngle' if angle else 'sides'
        values = {n: cmds.getAttr(n + '.'+attr) for n in nodes}
        widget=self.toolbox._edit_panel.options.angle_spin if angle else self.toolbox._edit_panel.options.spin
        initial=widget.value()
        cmds.undoInfo(openChunk=True, chunkName='Reset Tube '+attr)
        self.drag = dict(anchor=point, axis=None, values=values, start=initial,original=widget.value(),attr=attr,widget=widget)
        self.suppress_menu = True
        return True

    def move(self, point):
        drag = self.drag
        delta = (point.x() - drag['anchor'].x(), drag['anchor'].y() - point.y())
        if drag['axis'] is None:
            if max(map(abs, delta)) < 4: return
            drag['axis'] = 0 if abs(delta[0]) >= abs(delta[1]) else 1
        amount = delta[drag['axis']]
        step = math.floor(abs(amount) / 20) * (1 if amount >= 0 else -1)
        widget=drag['widget'];widget.setValue(max(widget.minimum(),min(widget.maximum(),drag['start']+step*(.5 if drag['attr']=='reductionAngle' else 1))))

    def finish(self, cancel=False):
        drag = self.drag
        if drag is None: return
        self.drag = None
        try:
            if cancel:
                for node, value in drag['values'].items():
                    if cmds.objExists(node): cmds.setAttr(node + '.'+drag['attr'], value)
                if drag['attr']=='sides':cmds.optionVar(intValue=('ModelPack_ResectSides', drag['original']))
                if self.toolbox._edit_panel is not None:
                    spin = drag['widget']
                    block = QtCore.QSignalBlocker(spin); spin.setValue(drag['original'])
        finally: cmds.undoInfo(closeChunk=True)

    def eventFilter(self, target, event):
        if self.closed or self.toolbox._edit_kind != 'reset_tube': return False
        kind = event.type()
        try:
            if kind in (QtCore.QEvent.KeyPress,QtCore.QEvent.ShortcutOverride):
                focus=QtWidgets.QApplication.focusWidget()
                editing=isinstance(target,(QtWidgets.QLineEdit,QtWidgets.QAbstractSpinBox,QtWidgets.QTextEdit,QtWidgets.QPlainTextEdit)) or isinstance(focus,(QtWidgets.QLineEdit,QtWidgets.QAbstractSpinBox,QtWidgets.QTextEdit,QtWidgets.QPlainTextEdit))
                undo=event.matches(QtGui.QKeySequence.Undo) or (event.key()==QtCore.Qt.Key_Z and not event.modifiers())
                redo=event.matches(QtGui.QKeySequence.Redo) or (event.key()==QtCore.Qt.Key_Z and event.modifiers()==QtCore.Qt.ShiftModifier)
                if not editing and (undo or redo):
                    if kind==QtCore.QEvent.KeyPress and not event.isAutoRepeat():
                        self.finish()
                        cmds.undo() if undo else cmds.redo()
                        self.toolbox._restore_modifier_controls()
                    event.accept();return True
            if self.drag is not None:
                if kind == QtCore.QEvent.MouseMove:
                    self.move(event.globalPosition().toPoint()); return True
                if kind == QtCore.QEvent.MouseButtonRelease and event.button() == QtCore.Qt.RightButton:
                    self.finish(); return True
                if kind == QtCore.QEvent.KeyPress and event.key() == QtCore.Qt.Key_Escape:
                    self.finish(cancel=True); return True
                if kind in (QtCore.QEvent.ApplicationDeactivate, QtCore.QEvent.WindowDeactivate): self.finish()
                if kind in (QtCore.QEvent.KeyPress, QtCore.QEvent.ShortcutOverride) and event.matches(QtGui.QKeySequence.Undo): self.finish()
            if kind == QtCore.QEvent.MouseButtonPress and event.button() == QtCore.Qt.RightButton:
                self.suppress_menu = False
                modifiers = event.modifiers()
                if not modifiers & QtCore.Qt.ShiftModifier or modifiers & QtCore.Qt.AltModifier: return False
                point = event.globalPosition().toPoint()
                if viewport_widget(target, point) is not None: return self.begin(point,angle=bool(modifiers&QtCore.Qt.ControlModifier))
            if kind == QtCore.QEvent.ContextMenu and self.suppress_menu:
                self.suppress_menu = False; return True
        except (ValueError, RuntimeError) as error:
            self.finish(cancel=True); cmds.warning('Reset Tube: ' + str(error))
        return False

    def close(self):
        if self.closed: return
        self.finish(); self.closed = True
        QtWidgets.QApplication.instance().removeEventFilter(self)
        om.MMessage.removeCallback(self.callback); self.deleteLater()
        for callback in self.scene_callbacks:om.MMessage.removeCallback(callback)
        self.scene_callbacks=[]
