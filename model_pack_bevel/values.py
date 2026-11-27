"""Compact numeric controls, synchronized with viewport gestures."""
from PySide6 import QtCore,QtWidgets
from .session import DEFAULT_SUPPORT_PERCENT, MAX_SUPPORT_EDGES


class _ConfirmNumber:
    def keyPressEvent(self,event):
        super().keyPressEvent(event)
        # A spin box lets Return propagate to its QDialog. There it can click
        # the toolbox's default Combine button and finish this Bevel session.
        if event.key() in (QtCore.Qt.Key_Return,QtCore.Qt.Key_Enter):event.accept()


class _IntegerField(_ConfirmNumber,QtWidgets.QSpinBox):pass
class _DecimalField(_ConfirmNumber,QtWidgets.QDoubleSpinBox):
    def __init__(self,parent=None):
        super().__init__(parent)
        self.setLocale(QtCore.QLocale.c())

    def validate(self,text,position):
        return super().validate(text.replace(',','.'),position)

    def valueFromText(self,text):
        return super().valueFromText(text.replace(',','.'))


class ValuesPanel(QtWidgets.QGroupBox):
    changed = QtCore.Signal(str,float)
    manual_requested = QtCore.Signal()
    manual_select_requested = QtCore.Signal()

    def __init__(self,parent=None):
        super().__init__("Bevel",parent)
        layout = QtWidgets.QGridLayout(self)
        layout.setColumnStretch(1,1)
        layout.setColumnStretch(3,1)
        self.fields = {}
        for name,label,row,col in (("width","Ширина",0,0),("segments","Сегменты",1,0),
                                  ("shape","Форма",0,2),("transverse_cuts","Резы",1,2),
                                  ("support_edges",'Support Edges',4,0),("support_percent","Шаг",4,2)):
            if name == "support_edges":
                field = _IntegerField(self)
                field.setRange(0, MAX_SUPPORT_EDGES)
                field.setToolTip("Рёбра снаружи фаски с каждой стороны. Жест Shift+ПКМ.")
            elif name == "support_percent":
                field = _DecimalField(self)
                field.setDecimals(3)
                field.setRange(0., 100.)
                field.setSingleStep(1.)
                field.setSuffix("%")
                field.setToolTip("Шаг в процентах от текущей ширины. Перетаскивание ПКМ или Ctrl+Shift+ПКМ. При изменении ширины расстояние сохраняется. 0% убирает опорные рёбра.")
            elif name in ("segments","transverse_cuts"):
                field = _IntegerField(self)
                field.setRange(1 if name=="segments" else 0,64)
            else:
                field = _DecimalField(self)
                field.setDecimals(4 if name == "width" else 3)
                field.setRange(0 if name=="width" else -1,1e9 if name=="width" else 1)
                field.setSingleStep(.0001 if name=="width" else .05)
            field.setKeyboardTracking(False)
            field.setMinimumWidth(70)
            field.setSizePolicy(QtWidgets.QSizePolicy.Ignored,QtWidgets.QSizePolicy.Fixed)
            # Keyboard tracking is off: typing commits once on Return/focus
            # loss, while arrows and wheel steps update the preview at once.
            field.valueChanged.connect(lambda value,n=name:self.changed.emit(n,float(value)))
            layout.addWidget(QtWidgets.QLabel(label,self),row,col)
            layout.addWidget(field,row,col+1)
            self.fields[name] = field
        for row,name,label,tip in ((2,"all_sections",'All Disconnected Bevels',
                "Shift+Ctrl изменяет продольную ширину во всех выбранных несвязанных фасках."),
                (3,"keep_equal",'Uniform Width',
                "Общий предел ширины. Выключите, чтобы каждый участок останавливался отдельно.")):
            check = QtWidgets.QCheckBox(label,self)
            check.setToolTip(tip)
            check.toggled.connect(lambda checked,n=name:self.changed.emit(n,float(checked)))
            layout.addWidget(check,row,0,1,4)
            self.fields[name] = check
        self.manual_button = QtWidgets.QPushButton('Edit Selected Faces', self)
        self.manual_select_button = QtWidgets.QPushButton('Pick Faces Manually', self)
        self.manual_select_button.setAutoDefault(False)
        self.manual_select_button.clicked.connect(lambda checked=False: self.manual_select_requested.emit())
        layout.addWidget(self.manual_select_button,5,0,1,4)
        self.manual_button.setAutoDefault(False)
        self.manual_button.setToolTip("Выделите всю полосу фаски полигонами. Редактируется только выделенный участок, без автоматического поиска и расширения.")
        self.manual_button.clicked.connect(lambda checked=False: self.manual_requested.emit())
        layout.addWidget(self.manual_button,6,0,1,4)

    def sync(self,state):
        self.manual_button.setEnabled(not hasattr(state, 'mesh'))
        self.manual_select_button.setEnabled(not hasattr(state, 'mesh'))
        recovered=getattr(state,'recovered',False)
        width_only=getattr(state,'width_only',False)
        joined=getattr(getattr(state, 'history_plan', None), 'joined', False)
        sharp=getattr(state,'sharp',False)
        for name in ('shape','segments'):
            self.fields[name].setEnabled(not width_only and not sharp)
            self.fields[name].setToolTip('Для этой сети фасок доступно изменение ширины.' if width_only else
                                         'Доступно в режиме со скруглением.' if sharp else '')
        editable_supports = not joined and (not recovered or getattr(state, 'can_edit_supports', False))
        self.fields['support_edges'].setEnabled(editable_supports)
        self.fields['support_percent'].setEnabled(editable_supports)
        self.setTitle('Bevel · редактирование' if recovered else 'Bevel · без скругления' if sharp else 'Bevel')
        for name in ('transverse_cuts','keep_equal'):
            self.fields[name].setEnabled(not recovered and not joined)
            if recovered:self.fields[name].setToolTip('Доступно при создании новой фаски.')
            else:self.fields[name].setToolTip('' if name=='transverse_cuts' else 'Общий предел ширины. Выключите, чтобы каждый участок останавливался отдельно.')
        for name,field in self.fields.items():
            blocker = QtCore.QSignalBlocker(field)
            if isinstance(field,QtWidgets.QCheckBox):
                field.setChecked(bool(getattr(state,name,False)))
            elif name == "support_percent":
                value = getattr(state, 'display_support_percent', None)
                if value is None:
                    value = getattr(state, name, None)
                if value is None:
                    value = DEFAULT_SUPPORT_PERCENT
                field.setMaximum(max(100., value))
                field.setValue(value)
            else:
                field.setValue(getattr(state,name,0))
            del blocker
