"""Compact numeric controls, synchronized with viewport gestures."""
from PySide6 import QtCore,QtWidgets


class ValuesPanel(QtWidgets.QGroupBox):
    changed = QtCore.Signal(str,float)

    def __init__(self,parent=None):
        super().__init__("Bevel",parent)
        layout = QtWidgets.QGridLayout(self)
        layout.setColumnStretch(1,1)
        layout.setColumnStretch(3,1)
        self.fields = {}
        self._edited = set()
        for name,label,row,col in (("width","Ширина",0,0),("segments","Сегменты",1,0),
                                  ("shape","Форма",0,2),("transverse_cuts","Резы",1,2)):
            if name in ("segments","transverse_cuts"):
                field = QtWidgets.QSpinBox(self)
                field.setRange(1 if name=="segments" else 0,64)
            else:
                field = QtWidgets.QDoubleSpinBox(self)
                field.setDecimals(6 if name=="width" else 3)
                field.setRange(0 if name=="width" else -1,1e9 if name=="width" else 1)
                field.setSingleStep(.01 if name=="width" else .05)
            field.setKeyboardTracking(False)
            field.setMinimumWidth(70)
            field.setSizePolicy(QtWidgets.QSizePolicy.Ignored,QtWidgets.QSizePolicy.Fixed)
            field.valueChanged.connect(lambda value,n=name:self._edited.add(n))
            field.editingFinished.connect(lambda n=name:self._finish(n))
            layout.addWidget(QtWidgets.QLabel(label,self),row,col)
            layout.addWidget(field,row,col+1)
            self.fields[name] = field
        for row,name,label,tip in ((2,"all_sections","Все несвязанные фаски",
                "Shift+Ctrl изменяет продольную ширину во всех выбранных несвязанных фасках."),
                (3,"keep_equal","Равная ширина",
                "Общий предел ширины. Выключите, чтобы каждый участок останавливался отдельно.")):
            check = QtWidgets.QCheckBox(label,self)
            check.setToolTip(tip)
            check.toggled.connect(lambda checked,n=name:self.changed.emit(n,float(checked)))
            layout.addWidget(check,row,0,1,4)
            self.fields[name] = check

    def _finish(self,name):
        if name in self._edited:
            self._edited.discard(name)
            self.changed.emit(name,self.fields[name].value())

    def sync(self,state):
        for name,field in self.fields.items():
            blocker = QtCore.QSignalBlocker(field)
            if isinstance(field,QtWidgets.QCheckBox):
                field.setChecked(bool(getattr(state,name,False)))
            else:
                field.setValue(getattr(state,name,0))
            self._edited.discard(name)
            del blocker
