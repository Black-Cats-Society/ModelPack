"""T opens cap choices only for selected editable Fast Draft cylinders."""
from maya import cmds
from PySide6 import QtCore, QtGui, QtWidgets
from shiboken6 import isValid, wrapInstance
from maya.api import OpenMayaUI as omui

from .cylinder_caps import CAP_NAMES, NODE_TYPE


def selected_nodes():
    result = []
    for selected in cmds.ls(sl=True, objectsOnly=True, long=True) or []:
        history = [selected] + (cmds.listHistory(selected, pruneDagObjects=True) or [])
        matches = [node for node in history if cmds.nodeType(node) == NODE_TYPE]
        if not matches:return []
        for node in matches:
            if node not in result:result.append(node)
    return result


def set_cap(nodes, mode):
    from .core import transaction
    with transaction('Top Cap'):
        for node in nodes:
            if cmds.objExists(node):cmds.setAttr(node + '.capType', mode)


def attach(window):
    detach(window)
    listener = CapMenuFilter(window)
    window._draft_cap_listener = listener
    QtWidgets.QApplication.instance().installEventFilter(listener)


def detach(window):
    listener = getattr(window, '_draft_cap_listener', None)
    window._draft_cap_listener = None
    if listener is not None and isValid(listener):
        QtWidgets.QApplication.instance().removeEventFilter(listener)
        if listener.menu is not None:listener.menu.close()
        listener.deleteLater()


class CapMenuFilter(QtCore.QObject):
    def __init__(self, window):
        super().__init__(window)
        self.owner = window
        self.menu = None
        self.held = False

    def owns(self, target):
        if not isinstance(target, QtWidgets.QWidget):return False
        if target is self.owner or self.owner.isAncestorOf(target):return True
        for panel in cmds.getPanel(type='modelPanel') or []:
            try:
                widget = wrapInstance(int(omui.M3dView.getM3dViewFromModelPanel(panel).widget()), QtWidgets.QWidget)
                if widget is not None and (target is widget or widget.isAncestorOf(target)):return True
            except RuntimeError:continue
        return False

    def eventFilter(self, target, event):
        if event.type() not in (QtCore.QEvent.KeyPress, QtCore.QEvent.KeyRelease):return False
        if event.key() != QtCore.Qt.Key_T:return False
        if event.type() == QtCore.QEvent.KeyRelease and self.held:
            self.held = False;event.accept();return True
        if event.type() != QtCore.QEvent.KeyPress or event.modifiers() != QtCore.Qt.NoModifier:return False
        fields = (QtWidgets.QLineEdit, QtWidgets.QTextEdit, QtWidgets.QPlainTextEdit,
                  QtWidgets.QAbstractSpinBox, QtWidgets.QComboBox)
        if isinstance(target, fields) or isinstance(QtWidgets.QApplication.focusWidget(), fields):return False
        if not self.owns(target):return False
        if getattr(self.owner, '_active_tool', None) not in (None, 'fast_draft'):return False
        from . import tool
        if tool._active is not None and tool._active.footprint is not None:return False
        nodes = selected_nodes()
        if not nodes:return False
        self.held = True
        if not event.isAutoRepeat():QtCore.QTimer.singleShot(0, lambda: self.show_menu(nodes))
        event.accept();return True

    def show_menu(self, nodes):
        if not isValid(self) or not isValid(self.owner):return
        if getattr(self.owner, '_draft_cap_listener', None) is not self or not self.owner.isVisible():return
        nodes = [node for node in nodes if cmds.objExists(node)]
        if not nodes:return
        import model_pack_fast_draft
        model_pack_fast_draft.shutdown()
        if self.menu is not None:self.menu.close();self.menu.deleteLater()
        self.menu = QtWidgets.QMenu('Cylinder · Top Cap', self.owner)
        group = QtGui.QActionGroup(self.menu);group.setExclusive(True)
        modes = {cmds.getAttr(node + '.capType') for node in nodes}
        for mode, label in enumerate(CAP_NAMES):
            action = self.menu.addAction(label)
            action.setCheckable(True);action.setChecked(modes == {mode});group.addAction(action)
            action.triggered.connect(lambda checked=False, value=mode: set_cap(nodes, value))
        self.menu.popup(QtGui.QCursor.pos())
