"""Local ReCurve-inspired appearance; never changes Maya's application palette."""
from PySide6 import QtCore, QtGui, QtWidgets


class Dialog(QtWidgets.QDialog):
    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setPen(QtGui.QPen(QtGui.QColor('#353940'), 1))
        painter.setBrush(QtGui.QColor('#282b30'))
        painter.drawRoundedRect(QtCore.QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 13, 13)
        painter.end()


STYLE = """
QDialog { background: #282b30; border: 1px solid #353940; border-radius: 13px; }
QWidget { color: #f1f2f3; font-family: 'Segoe UI'; font-size: 12px; }
QLabel { background: transparent; border: none; }
QPushButton, QToolButton {
    background: #393d43; border: 1px solid #464b53; border-radius: 9px;
    padding: 5px 9px; min-height: 20px;
}
QPushButton:hover, QToolButton:hover { background: #454a51; border-color: #68717a; }
QPushButton:pressed, QToolButton:pressed { background: #30353a; }
QPushButton:checked, QPushButton[primary="true"] {
    background: #d3e4dd; color: #202c32; border-color: #d3e4dd;
}
QPushButton:checked:hover, QPushButton[primary="true"]:hover { background: #e1eee8; }
QPushButton#modelPackTool { background:#353b41; border-color:#464e50; border-radius:6px; }
QPushButton#modelPackTool:hover { background:#40484b; border-color:#75867e; }
QPushButton#modelPackTool:checked { background:#3c514a; border-color:#779786; color:#e5e9e7; }
QPushButton#modelPackTool:checked:hover { background:#465b53; }
QWidget#modelPackOffsetSettings QSlider::groove:horizontal, QGroupBox#modelPackNonplanarHP QSlider::groove:horizontal { height:4px; background:#444e53; border-radius:2px; }
QWidget#modelPackOffsetSettings QSlider::sub-page:horizontal, QGroupBox#modelPackNonplanarHP QSlider::sub-page:horizontal { background:#779786; border-radius:2px; }
QWidget#modelPackOffsetSettings QSlider::handle:horizontal, QGroupBox#modelPackNonplanarHP QSlider::handle:horizontal { width:9px; margin:-4px 0; background:#d3e4dd; border-radius:3px; }
QWidget#modelPackOffsetSettings QSlider::handle:horizontal:disabled { background:#626b72; }
QWidget#modelPackOffsetSettings QSlider::sub-page:horizontal:disabled { background:#4c565a; }
QPushButton:disabled { color: #797f87; background: #303339; border-color: #3b3f45; }
QSpinBox, QDoubleSpinBox, QComboBox, QLineEdit {
    background: #393d43; color: #f1f2f3; border: 1px solid #464b53;
    border-radius: 9px; padding: 5px 8px; min-height: 20px;
    selection-background-color: #d3e4dd; selection-color: #202c32;
}
QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus, QLineEdit:focus { border-color: #a6c7bb; }
QSpinBox::up-button, QDoubleSpinBox::up-button { width: 19px; border: none; }
QSpinBox::down-button, QDoubleSpinBox::down-button { width: 19px; border: none; }
QGroupBox { border: 1px solid #464b53; border-radius: 9px; margin-top: 10px; padding: 9px; }
QGroupBox::title { subcontrol-origin: margin; left: 9px; padding: 0 4px; }
QListWidget { background: #30343a; border: 1px solid #464b53; border-radius: 9px; padding: 4px; }
QListWidget::item { padding: 5px; border-radius: 4px; }
QListWidget::item:selected { background: #d3e4dd; color: #202c32; }
QCheckBox { spacing: 7px; background: transparent; }
QCheckBox::indicator { width: 14px; height: 14px; border: 1px solid #69717a; border-radius: 4px; background: #393d43; }
QCheckBox::indicator:checked { background: #d3e4dd; border-color: #d3e4dd; }
QMenu { background: #282b30; border: 1px solid #464b53; padding: 6px; }
QMenu::item { padding: 6px 20px; border-radius: 5px; }
QMenu::item:selected { background: #454a51; }
QMenu::separator { height: 1px; background: #464b53; margin: 5px; }
QToolTip { background: #282b30; color: #e5e9ed; border: 1px solid #555e66; padding: 5px; }
QFrame#modelPackHeader { background: transparent; border: none; }
QLabel#modelPackTitle { font-size: 15px; }
QPushButton#modelPackClose { background: transparent; color: #a3aab4; border: none; font-size: 20px; padding: 0; min-height: 24px; }
QPushButton#modelPackClose:hover { color: white; background: #454a51; }
"""


class TitleBar(QtWidgets.QFrame):
    """A draggable compact title bar with a real close action."""
    def __init__(self, window):
        super().__init__(window)
        self.setObjectName('modelPackHeader')
        self._window = window
        self._offset = None
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 7)
        title = QtWidgets.QLabel('Model Pack', self)
        title.setObjectName('modelPackTitle')
        title.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)
        layout.addWidget(title)
        layout.addStretch()
        close = QtWidgets.QPushButton('\u00d7', self)
        close.setObjectName('modelPackClose')
        close.setFixedSize(24, 24)
        close.setToolTip('Закрыть')
        close.setAutoDefault(False)
        close.clicked.connect(window.close)
        layout.addWidget(close)

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self._offset = event.globalPosition().toPoint() - self._window.pos()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._offset is not None and event.buttons() & QtCore.Qt.LeftButton:
            self._window.move(event.globalPosition().toPoint() - self._offset)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._offset = None
        super().mouseReleaseEvent(event)


class UndoGroup:
    """Native commands stay stepwise while open and collapse when finished."""
    def __init__(self, label, on_scene_close=None):
        from maya import cmds
        from maya.api import OpenMaya as om
        self.open=True;self.callbacks=[]
        cmds.undoInfo(openChunk=True,chunkName=label)
        if on_scene_close is not None:
            self.callbacks=[om.MSceneMessage.addCallback(kind,lambda *_:on_scene_close()) for kind in
                (om.MSceneMessage.kBeforeNew,om.MSceneMessage.kBeforeOpen,om.MSceneMessage.kMayaExiting)]
    def close(self):
        if not self.open:return
        self.open=False
        controller=getattr(QtWidgets.QApplication.instance(),'_model_pack_parameter_drag',None)
        if controller is not None:controller.finish()
        from maya import cmds
        from maya.api import OpenMaya as om
        for callback in self.callbacks:om.MMessage.removeCallback(callback)
        self.callbacks=[];cmds.undoInfo(closeChunk=True)


class PreviewHistory(QtCore.QObject):
    """Undo settings on transient previews without touching older scene edits."""
    def __init__(self, host, fields, apply):
        super().__init__(host);self.host=host;self.fields=tuple(fields);self.apply=apply
        self.undo=[];self.redo=[];self.group=False;self.restoring=False;self.closed=False
        self.current=self.snapshot()
        for field in self.fields:
            (field.toggled if isinstance(field,QtWidgets.QCheckBox) else field.valueChanged).connect(self.record)
        QtWidgets.QApplication.instance().installEventFilter(self)
    def snapshot(self):
        return tuple(f.isChecked() if isinstance(f,QtWidgets.QCheckBox) else f.value() for f in self.fields)
    def record(self,*_):
        if self.restoring or self.closed or self.group:return
        state=self.snapshot()
        if state!=self.current:self.undo.append(self.current);self.redo=[];self.current=state
    def begin(self):
        if not self.group:self.record();self.group=True
    def end(self):
        self.group=False;self.record()
    def restore(self,redo=False):
        controller=getattr(QtWidgets.QApplication.instance(),'_model_pack_parameter_drag',None)
        if controller is not None and controller.drag is not None:controller.finish()
        if hasattr(self.host,'finish_drag'):self.host.finish_drag()
        if hasattr(self.host,'anchor'):self.host.anchor=None
        self.end();source=self.redo if redo else self.undo;target=self.undo if redo else self.redo
        if not source:return
        state=source.pop();target.append(self.current);self.restoring=True
        try:
            blockers=[QtCore.QSignalBlocker(field) for field in self.fields]
            for field,value in zip(self.fields,state):
                field.setChecked(value) if isinstance(field,QtWidgets.QCheckBox) else field.setValue(value)
            del blockers
            self.apply()
            if not self.closed:self.current=self.snapshot()
        finally:self.restoring=False
    def eventFilter(self,obj,event):
        if self.closed or self.host.stopped:return False
        kind=event.type();window=self.host.window()
        belongs=isinstance(obj,QtWidgets.QWidget) and window is not None and (obj is window or window.isAncestorOf(obj))
        if kind==QtCore.QEvent.MouseButtonPress and belongs and event.button()==QtCore.Qt.LeftButton:self.begin()
        elif kind==QtCore.QEvent.MouseButtonRelease and self.group:QtCore.QTimer.singleShot(0,self.end)
        elif kind==QtCore.QEvent.ApplicationDeactivate:self.end()
        if kind not in (QtCore.QEvent.KeyPress,QtCore.QEvent.ShortcutOverride):return False
        inputs=(QtWidgets.QLineEdit,QtWidgets.QAbstractSpinBox,QtWidgets.QTextEdit,QtWidgets.QPlainTextEdit)
        if isinstance(obj,inputs) or isinstance(QtWidgets.QApplication.focusWidget(),inputs):return False
        undo=event.matches(QtGui.QKeySequence.Undo) or (event.key()==QtCore.Qt.Key_Z and not event.modifiers())
        redo=event.matches(QtGui.QKeySequence.Redo) or (event.key()==QtCore.Qt.Key_Z and event.modifiers()==QtCore.Qt.ShiftModifier)
        if not (undo or redo):return False
        if not belongs:
            from model_pack_tube_viewport import viewport_widget
            belongs=viewport_widget(obj,QtGui.QCursor.pos()) is not None
        if not belongs:return False
        if kind==QtCore.QEvent.KeyPress and not event.isAutoRepeat():self.restore(redo)
        event.accept();return True
    def close(self):
        if self.closed:return
        controller=getattr(QtWidgets.QApplication.instance(),'_model_pack_parameter_drag',None)
        if controller is not None:controller.finish()
        self.end();self.closed=True
        QtWidgets.QApplication.instance().removeEventFilter(self)


class ParameterDrag(QtCore.QObject):
    """Shared label scrubbing for numeric controls, including dynamic panels."""
    def __init__(self, app):
        super().__init__(app)
        self.windows = []
        self.drag = None
        app.installEventFilter(self)

    def field(self, label):
        import shiboken6
        self.windows = [w for w in self.windows if shiboken6.isValid(w)]
        owner = next((w for w in self.windows if w is label or w.isAncestorOf(label)), None)
        if owner is None or not isinstance(label, QtWidgets.QLabel):
            return None
        buddy = label.buddy()
        if isinstance(buddy, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)):
            return buddy
        # Layout geometry also handles several label/field pairs in one row.
        rect = QtCore.QRect(label.mapTo(owner, QtCore.QPoint()), label.size())
        candidates = []
        for field in owner.findChildren(QtWidgets.QAbstractSpinBox):
            if not isinstance(field, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)):
                continue
            if not field.isVisible() or not field.isEnabled() or field.isReadOnly():
                continue
            box = QtCore.QRect(field.mapTo(owner, QtCore.QPoint()), field.size())
            if box.left() >= rect.right() - 2 and abs(box.center().y() - rect.center().y()) <= 12:
                candidates.append((box.left() - rect.right(), field))
        return min(candidates, key=lambda item:item[0])[1] if candidates else None

    def finish(self):
        if self.drag is None:
            return
        label, field, *_ = self.drag
        self.drag = None
        import shiboken6
        if shiboken6.isValid(label):
            label.releaseMouse()
        from maya import cmds
        cmds.undoInfo(closeChunk=True)

    def eventFilter(self, obj, event):
        kind = event.type()
        if self.drag is not None:
            label, field, start, value = self.drag
            import shiboken6
            if kind == QtCore.QEvent.Close and isinstance(obj, QtWidgets.QWidget) and obj.isAncestorOf(label):
                self.finish()
                return False
            if not shiboken6.isValid(field) or kind == QtCore.QEvent.ApplicationDeactivate:
                self.finish()
                return False
            if obj is label and kind == QtCore.QEvent.MouseMove:
                factor = .1 if event.modifiers() & QtCore.Qt.ShiftModifier else 1.
                if event.modifiers() & QtCore.Qt.ControlModifier:
                    factor *= 10.
                amount = (event.globalPosition().x() - start) / 8. * field.singleStep() * factor
                result = value + amount
                field.setValue(round(result) if isinstance(field, QtWidgets.QSpinBox) else result)
                return True
            if obj is label and kind == QtCore.QEvent.MouseButtonRelease:
                self.finish()
                return True
            if obj is label and kind == QtCore.QEvent.Hide:
                self.finish()
        if isinstance(obj, QtWidgets.QLabel):
            if kind == QtCore.QEvent.Enter:
                if self.field(obj) is not None:
                    obj.setCursor(QtCore.Qt.SizeHorCursor)
                else:
                    obj.unsetCursor()
            elif kind == QtCore.QEvent.MouseButtonPress and event.button() == QtCore.Qt.LeftButton:
                field = self.field(obj)
                if field is not None:
                    from maya import cmds
                    cmds.undoInfo(openChunk=True, chunkName='Model Pack parameter')
                    self.drag = (obj, field, event.globalPosition().x(), field.value())
                    obj.grabMouse()
                    return True
        return False


def apply(window):
    from model_pack_help import install
    install(window)
    window.setWindowFlag(QtCore.Qt.FramelessWindowHint, True)
    window.setAttribute(QtCore.Qt.WA_TranslucentBackground)
    window.setStyleSheet(STYLE)
    app = QtWidgets.QApplication.instance()
    controller = getattr(app, '_model_pack_parameter_drag', None)
    if controller is None:
        controller = ParameterDrag(app)
        app._model_pack_parameter_drag = controller
    controller.windows.append(window)
