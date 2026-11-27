"""Rearrangeable Qt panel with screen placement and dragging. No mesh mutations."""
try:
    from PySide6 import QtCore, QtWidgets, QtGui
except ImportError:
    from PySide2 import QtCore, QtWidgets, QtGui


STYLES = {
    'Graphite': dict(bg='#2b2d31', field='#3b3e44', border='#454950', text='#eef0f2',
                     muted='#a4abb5', accent='#d4e3dc', ink='#202a25', hover='#494e56'),
    'Midnight': dict(bg='#182131', field='#25334a', border='#34455e', text='#e8f2ff',
                     muted='#9aaec8', accent='#80cfff', ink='#152738', hover='#344963'),
    'Ember': dict(bg='#302824', field='#453831', border='#58473e', text='#fff0e5',
                  muted='#c0a99a', accent='#f2bd82', ink='#38281a', hover='#59483d'),
}

LAYOUTS = {
    'Mini Tower': 'A narrow vertical panel with visible shortcuts.',
    'Compact': 'Inline controls in a narrow card.',
    'Split': 'Mesh settings and curve controls in separate columns.',
    'Toolbar': 'A horizontal strip for wide viewports.',
}


class IconButton(QtWidgets.QToolButton):
    def __init__(self, kind, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.setFixedSize(26, 26)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setFocusPolicy(QtCore.Qt.NoFocus)

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        color = self.palette().color(QtGui.QPalette.ButtonText)
        p.setPen(QtGui.QPen(color, 1.5, QtCore.Qt.SolidLine, QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin))
        if self.kind == 'close':
            p.drawLine(9, 9, 17, 17);p.drawLine(17, 9, 9, 17)
        elif self.kind == 'options':
            p.setBrush(color);p.setPen(QtCore.Qt.NoPen)
            for x in (7, 13, 19):p.drawEllipse(QtCore.QPointF(x, 13), 1.3, 1.3)
        elif self.kind in ('minus', 'plus'):
            p.drawLine(8, 13, 18, 13)
            if self.kind == 'plus':p.drawLine(13, 8, 13, 18)
        else:
            p.drawLine(8, 11, 13, 16);p.drawLine(13, 16, 18, 11)
        p.end()


class CurveMark(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(23, 24)

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.scale(self.width()/23.0, self.height()/24.0)
        color = self.palette().color(QtGui.QPalette.WindowText)
        p.setPen(QtGui.QPen(color, 1.7))
        path = QtGui.QPainterPath()
        path.moveTo(3, 19);path.cubicTo(6, 18, 9, 4, 20, 5)
        p.drawPath(path)
        p.setBrush(color)
        for x, y in ((3, 19), (10, 10), (20, 5)):p.drawEllipse(QtCore.QPointF(x, y), 1.8, 1.8)
        p.end()


class NumberField(QtWidgets.QFrame):
    def __init__(self, minimum, maximum, value, presets, parent=None):
        super().__init__(parent)
        self.setObjectName('numberField')
        self.setFixedSize(76, 28)
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(8, 0, 3, 0);row.setSpacing(0)
        self.minus = IconButton('minus', self);self.minus.setToolTip('Уменьшить значение на один шаг.')
        row.addWidget(self.minus);self.minus.hide()
        self.spin = QtWidgets.QSpinBox(self)
        self.spin.setRange(minimum, maximum);self.spin.setValue(value)
        self.spin.setButtonSymbols(QtWidgets.QAbstractSpinBox.NoButtons)
        self.spin.setAlignment(QtCore.Qt.AlignCenter)
        self.spin.setFocusPolicy(QtCore.Qt.ClickFocus)
        row.addWidget(self.spin, 1)
        self.arrow = IconButton('down', self)
        self.arrow.setFixedSize(22, 26)
        row.addWidget(self.arrow)
        self.plus = IconButton('plus', self);self.plus.setToolTip('Увеличить значение на один шаг.')
        row.addWidget(self.plus);self.plus.hide()
        self.minus.clicked.connect(self.spin.stepDown)
        self.plus.clicked.connect(self.spin.stepUp)
        self.presets = presets
        self.arrow.clicked.connect(self.show_presets)

    def set_tower(self, tower):
        self.minus.setVisible(tower);self.plus.setVisible(tower);self.arrow.setVisible(not tower)
        self.spin.setProperty('tower', tower)
        self.spin.style().unpolish(self.spin);self.spin.style().polish(self.spin)
        if tower:
            self.setMinimumWidth(0);self.setMaximumWidth(16777215);self.setFixedHeight(30)
            self.layout().setContentsMargins(4, 1, 4, 1)
        else:
            self.setFixedSize(76, 28);self.layout().setContentsMargins(8, 0, 3, 0)

    def show_presets(self):
        menu = QtWidgets.QMenu(self)
        for value in self.presets:
            if not self.spin.minimum() <= value <= self.spin.maximum():continue
            action = menu.addAction(str(value))
            action.setCheckable(True);action.setChecked(self.spin.value() == value)
            action.triggered.connect(lambda checked=False, v=value:self.spin.setValue(v))
        menu.popup(self.mapToGlobal(QtCore.QPoint(0, self.height()+4)))


class ComboField(QtWidgets.QComboBox):
    def paintEvent(self,event):
        super().paintEvent(event)
        p=QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.setPen(QtGui.QPen(self.palette().color(QtGui.QPalette.Text),1.4,QtCore.Qt.SolidLine,QtCore.Qt.RoundCap))
        x,y=self.width()-14,self.height()/2
        p.drawLine(QtCore.QPointF(x-3,y-1.5),QtCore.QPointF(x,y+1.5))
        p.drawLine(QtCore.QPointF(x,y+1.5),QtCore.QPointF(x+3,y-1.5))
        p.end()


class ActionButton(QtWidgets.QPushButton):
    """Native button with an optional second line for its shortcut."""
    shortcut = ''

    def paintEvent(self, event):
        if not self.shortcut:
            super().paintEvent(event);return
        option = QtWidgets.QStyleOptionButton();self.initStyleOption(option);option.text = ''
        painter = QtWidgets.QStylePainter(self)
        painter.drawControl(QtWidgets.QStyle.CE_PushButton, option)
        color = self.palette().color(QtGui.QPalette.ButtonText)
        painter.setPen(color)
        font = self.font();font.setPixelSize(12);painter.setFont(font)
        painter.drawText(QtCore.QRect(0, 1, self.width(), 17), QtCore.Qt.AlignCenter, self.text())
        font.setPixelSize(11);font.setWeight(QtGui.QFont.Normal);painter.setFont(font)
        color.setAlpha(190);painter.setPen(color)
        painter.drawText(QtCore.QRect(0, 18, self.width(), 14), QtCore.Qt.AlignCenter, self.shortcut)


class FeedbackLabel(QtWidgets.QLabel):
    """Only errors and brief confirmations occupy space in the panel."""
    def __init__(self, panel):
        super().__init__('', panel)
        self.panel = panel
        self.notice_timer = QtCore.QTimer(self);self.notice_timer.setSingleShot(True)
        self.notice_timer.timeout.connect(lambda: self.setText(''))
        self.hide()

    def setText(self, text):
        self.notice_timer.stop()
        super().setText(text)
        self.setVisible(bool(text))
        fit_panel(self.panel)

    def show_notice(self, text):
        self.setText(text);self.notice_timer.start(1500)


def panel_position(cursor, size, bounds):
    """Keep the panel beside the cursor and inside the current screen."""
    gap = 22
    x = cursor.x()+gap
    if x+size.width() > bounds.right()-8:x = cursor.x()-size.width()-gap
    y = cursor.y()+14
    return QtCore.QPoint(max(bounds.left()+8, min(x, bounds.right()-size.width()-8)),
                        max(bounds.top()+8, min(y, bounds.bottom()-size.height()-8)))


class PanelPlacement(QtCore.QObject):
    """Place on explicit opening; otherwise move only by dragging the header."""
    def __init__(self, panel):
        super().__init__(panel)
        self.panel = panel
        self.drag = None
        panel.installEventFilter(self)
        for widget in (panel.header, panel.title, panel.mark):widget.installEventFilter(self)

    def bounds(self, position):
        screen = QtWidgets.QApplication.screenAt(position) or QtWidgets.QApplication.primaryScreen()
        return screen.availableGeometry()

    def place(self, position=None):
        position = QtGui.QCursor.pos() if position is None else position
        self.drag = None
        self.panel.adjustSize()
        self.panel.move(panel_position(position, self.panel.size(), self.bounds(position)))

    def eventFilter(self, obj, event):
        if obj is self.panel:
            if event.type() in (QtCore.QEvent.Hide, QtCore.QEvent.Close):self.drag = None
            elif event.type()==QtCore.QEvent.Resize and self.panel.isVisible():
                bounds=self.bounds(self.panel.pos());pos=self.panel.pos()
                self.panel.move(max(bounds.left()+8,min(pos.x(),bounds.right()-self.panel.width()-8)),
                                max(bounds.top()+8,min(pos.y(),bounds.bottom()-self.panel.height()-8)))
            return False
        if event.type() == QtCore.QEvent.MouseButtonPress and event.button() == QtCore.Qt.LeftButton:
            point = event.globalPosition().toPoint() if hasattr(event, 'globalPosition') else event.globalPos()
            self.drag = point-self.panel.pos()
            return True
        if event.type() == QtCore.QEvent.MouseMove and self.drag is not None:
            point = event.globalPosition().toPoint() if hasattr(event, 'globalPosition') else event.globalPos()
            bounds = self.bounds(point);pos = point-self.drag
            self.panel.move(max(bounds.left(), min(pos.x(), bounds.right()-self.panel.width())),
                            max(bounds.top(), min(pos.y(), bounds.bottom()-self.panel.height())))
            return True
        if event.type() == QtCore.QEvent.MouseButtonRelease and self.drag is not None:
            self.drag = None
            return True
        return False


def set_style(panel, name):
    colors = STYLES[name if name in STYLES else 'Graphite']
    panel.setStyleSheet('''
        QDialog { background: transparent; }
        QFrame#panelSurface { background: %(bg)s; border: 1px solid %(border)s; border-radius: 13px; }
        QWidget { color: %(text)s; font-family: 'Segoe UI'; font-size: 12px; }
        QLabel { background: transparent; border: none; }
        QLabel#brand { font-size: 15px; font-weight: 600; }
        QLabel#section { font-size: 11px; font-weight: 600; }
        QLabel#shortcut, QLabel#footer { color: %(muted)s; font-size: 11px; }
        QLabel#liveBadge { color: %(accent)s; font-size: 9px; font-weight: 600; }
        QFrame#divider { background: %(border)s; border: none; max-height: 1px; }
        QFrame#groupDivider { background: %(border)s; border: none; }
        QFrame#numberField { background: %(field)s; border: 1px solid %(border)s; border-radius: 8px; }
        QSpinBox { background: transparent; border: none; padding: 1px; font-size: 13px; font-weight: 600; min-width: 30px; selection-background-color: %(accent)s; selection-color: %(ink)s; }
        QSpinBox[tower="true"] { font-size: 16px; font-weight: 500; }
        QComboBox { background: %(field)s; border: 1px solid %(border)s; border-radius: 8px; padding: 3px 23px 3px 9px; }
        QComboBox::drop-down { border: none; width: 21px; }
        QComboBox::down-arrow { image: none; width: 0px; height: 0px; }
        QComboBox QAbstractItemView { background: %(field)s; color: %(text)s; selection-background-color: %(hover)s; border: 1px solid %(border)s; padding: 4px; }
        QPushButton { background: %(field)s; border: 1px solid %(border)s; border-radius: 8px; padding: 4px 9px; font-weight: 500; }
        QPushButton:hover, QToolButton:hover { background: %(hover)s; }
        QPushButton:pressed { background: %(border)s; }
        QPushButton#primary { background: %(accent)s; color: %(ink)s; border: none; font-weight: 600; }
        QPushButton#primary:hover { border: 1px solid %(text)s; }
        QPushButton#subtle { background: transparent; padding: 4px 8px; color: %(muted)s; font-size: 11px; }
        QToolButton { background: transparent; color: %(muted)s; border: none; border-radius: 6px; }
        QToolButton:checked { background: %(field)s; color: %(accent)s; }
        QToolButton::menu-indicator { image: none; width: 0px; }
        QComboBox#layoutPicker { font-size: 10px; background: transparent; padding: 3px 21px 3px 7px; border-radius: 7px; }
        QLabel#feedback { color: %(muted)s; font-size: 11px; }
        QMenu { background: %(field)s; color: %(text)s; border: 1px solid %(border)s; padding: 5px; }
        QMenu::item { padding: 4px 16px; }
        QMenu::item:selected { background: %(hover)s; }
        QToolTip { background: %(field)s; color: %(text)s; border: 1px solid %(border)s; padding: 5px; }
    ''' % colors)
    for key, action in getattr(panel, 'style_actions', {}).items():action.setChecked(key == name)
    panel.update()


def _empty(layout):
    """Detach layout items without destroying shared controls or their signals."""
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():item.widget().hide()


def fit_panel(panel):
    panel.surface_layout.activate()
    panel.layout().activate()
    panel.adjustSize()


def set_layout(panel, name):
    """Reposition the same widgets, retaining the active editing session."""
    name = name if name in LAYOUTS else 'Mini Tower'
    panel.layout_name = name
    previous = panel.updatesEnabled()
    panel.setUpdatesEnabled(False)
    try:
        for layout in (panel.header.layout(), panel.body.layout(), panel.footer.layout(), panel.surface_layout):
            _empty(layout)
        body = panel.body.layout()
        # Grid stretches and minimums survive takeAt(), so clear them as well.
        for index in range(body.columnCount()):
            body.setColumnStretch(index, 0);body.setColumnMinimumWidth(index, 0)
        for index in range(body.rowCount()):body.setRowStretch(index, 0)
        header = panel.header.layout()
        panel.footer.hide()
        if name == 'Mini Tower':
            for widget in (panel.live_badge, panel.options, panel.layout_picker):widget.hide()
        def add(layout, widget, *args):
            layout.addWidget(widget, *args);widget.show()
        panel.mark.setFixedSize(18, 20) if name=='Mini Tower' else panel.mark.setFixedSize(23, 24)
        add(header, panel.mark)
        add(header, panel.title)
        if name != 'Mini Tower':add(header, panel.live_badge)
        header.addStretch(1)
        if name == 'Toolbar':add(header, panel.layout_picker)
        if name != 'Mini Tower':add(header, panel.options)
        add(header, panel.close_button)
        footer = panel.footer.layout()
        for widget in (panel.segment_label, panel.control_label, panel.segment_caption,
                       panel.points_caption, panel.key_hint, panel.divider_a, panel.divider_b,
                       panel.segment_shortcut, panel.control_shortcut, panel.rule_a, panel.rule_b):widget.hide()
        tower = name == 'Mini Tower'
        for field in (panel.segment_field, panel.curve_field):field.set_tower(tower)
        actions = panel.commit.layout();_empty(actions)
        actions.setDirection(QtWidgets.QBoxLayout.TopToBottom if tower else QtWidgets.QBoxLayout.LeftToRight)
        for widget in ((panel.apply, panel.cancel) if tower else (panel.cancel, panel.apply)):add(actions, widget)
        panel.apply.shortcut = 'Space / Enter' if tower else ''
        panel.cancel.shortcut = 'Esc' if tower else ''
        for widget in (panel.apply, panel.cancel):widget.setFixedHeight(34 if tower else 28)
        for widget in (panel.reset_curves, panel.remove_curves):widget.setFixedHeight(24 if tower else 28)
        panel.segment_label.setAlignment(QtCore.Qt.AlignCenter if tower else QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        panel.control_label.setAlignment(QtCore.Qt.AlignCenter if tower else QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        panel.mode.setFixedHeight(28)
        panel.mode.setMinimumWidth(0);panel.mode.setMaximumWidth(16777215)
        if tower:
            panel.setFixedWidth(200)
            panel.mode.setFixedHeight(28)
            for row, widget in enumerate((panel.segment_label, panel.segment_shortcut, panel.segment_field,
                                          panel.mode, panel.rule_a, panel.control_label, panel.control_shortcut,
                                          panel.curve_field, panel.curve_actions,
                                          panel.rule_b, panel.commit)):
                add(body, widget, row, 0)
            body.setColumnStretch(0, 1)
        elif name == 'Compact':
            panel.setFixedWidth(316)
            add(body, panel.segment_label, 0, 0)
            add(body, panel.segment_field, 0, 1)
            add(body, panel.mode, 0, 2)
            add(body, panel.control_label, 1, 0)
            add(body, panel.curve_field, 1, 1, 1, 2, QtCore.Qt.AlignRight)
            add(body, panel.curve_actions, 2, 1, 1, 2)
            add(body, panel.commit, 3, 0, 1, 3)
            body.setColumnStretch(2, 1)
            add(footer, panel.layout_picker)
            footer.addStretch(1)
            add(footer, panel.key_hint)
        elif name == 'Split':
            panel.setFixedWidth(420)
            panel.segment_label.setText('Mesh segments')
            panel.control_label.setText('Control curves')
            add(body, panel.segment_label, 0, 0, 1, 2)
            add(body, panel.control_label, 0, 3, 1, 2)
            add(body, panel.segment_caption, 1, 0)
            add(body, panel.segment_field, 1, 1)
            add(body, panel.points_caption, 1, 3)
            add(body, panel.curve_field, 1, 4)
            add(body, panel.mode, 2, 0, 1, 2)
            add(body, panel.curve_actions, 2, 3, 1, 2)
            add(body, panel.divider_a, 0, 2, 4, 1)
            body.setColumnStretch(0, 1);body.setColumnStretch(3, 1)
            body.setColumnMinimumWidth(2, 14)
            add(footer, panel.layout_picker)
            footer.addStretch(1)
            add(footer, panel.commit)
        else:
            panel.setFixedWidth(540)
            add(body, panel.segment_label, 0, 0, 1, 2)
            add(body, panel.segment_field, 1, 0)
            add(body, panel.mode, 1, 1)
            add(body, panel.divider_a, 0, 2, 3, 1)
            add(body, panel.control_label, 0, 3, 1, 2)
            add(body, panel.curve_field, 1, 3)
            add(body, panel.curve_actions, 2, 3, 1, 2)
            add(body, panel.divider_b, 0, 5, 3, 1)
            add(body, panel.key_hint, 0, 6)
            add(body, panel.commit, 1, 6)
            body.setColumnStretch(1, 1)
            body.setColumnMinimumWidth(2, 14);body.setColumnMinimumWidth(5, 14)
        if name != 'Split':
            panel.segment_label.setText('Segments');panel.control_label.setText('Curve CVs')
        panel.curve_actions.setVisible(panel.controls_active)
        panel.feedback.setMaximumWidth(panel.width()-44)
        for widget in (panel.header, panel.body):add(panel.surface_layout, widget)
        add(panel.surface_layout, panel.feedback);panel.feedback.setVisible(bool(panel.feedback.text()))
        # Toolbar's selector lives in the header, so it needs no footer.
        if name not in ('Toolbar', 'Mini Tower'):add(panel.surface_layout, panel.footer)
        blocked = panel.layout_picker.blockSignals(True)
        panel.layout_picker.setCurrentText(name);panel.layout_picker.blockSignals(blocked)
        for key, action in panel.layout_actions.items():action.setChecked(key == name)
        fit_panel(panel)
    finally:
        panel.setUpdatesEnabled(previous)
    panel.update()


def build_panel(panel, connected, version, style='Graphite', layout='Mini Tower'):
    panel.setWindowFlags(QtCore.Qt.Tool | QtCore.Qt.FramelessWindowHint)
    panel.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
    panel.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
    panel.setWindowTitle('ReCurve '+version)
    outer = QtWidgets.QVBoxLayout(panel)
    outer.setContentsMargins(10, 8, 10, 10)
    surface = QtWidgets.QFrame(panel);surface.setObjectName('panelSurface');outer.addWidget(surface)
    shadow = QtWidgets.QGraphicsDropShadowEffect(surface)
    shadow.setBlurRadius(20);shadow.setOffset(0, 4);shadow.setColor(QtGui.QColor(0, 0, 0, 90))
    surface.setGraphicsEffect(shadow)
    panel.surface_layout = QtWidgets.QVBoxLayout(surface)
    panel.surface_layout.setContentsMargins(12, 10, 12, 10);panel.surface_layout.setSpacing(6)
    panel.body = QtWidgets.QWidget(surface)
    body = QtWidgets.QGridLayout(panel.body);body.setContentsMargins(0, 0, 0, 0);body.setSpacing(6)
    panel.footer = QtWidgets.QWidget(surface)
    footer = QtWidgets.QHBoxLayout(panel.footer);footer.setContentsMargins(0, 0, 0, 0);footer.setSpacing(6)
    panel.controls_active = False
    panel.header = QtWidgets.QFrame(surface)
    header = QtWidgets.QHBoxLayout(panel.header);header.setContentsMargins(0, 0, 0, 0);header.setSpacing(4)
    panel.mark = CurveMark(panel.header)
    panel.title = QtWidgets.QLabel('ReCurve');panel.title.setObjectName('brand')
    panel.title.setToolTip('ReCurve '+version+' | Shift+R')
    panel.live_badge = QtWidgets.QLabel('LIVE');panel.live_badge.setObjectName('liveBadge')
    panel.options = IconButton('options');panel.options.setToolTip('Горячие клавиши и оформление.')
    panel.close_button = IconButton('close');panel.close_button.setToolTip('Отменить текущую операцию и закрыть настройки: Esc.')
    panel.close_button.clicked.connect(panel.close)

    def label(text, name='section'):
        widget = QtWidgets.QLabel(text, surface);widget.setObjectName(name);return widget
    panel.segment_label = label('Segments')
    panel.control_label = label('Curve CVs')
    panel.segment_caption = label('Count', 'shortcut')
    panel.points_caption = label('CVs', 'shortcut')
    panel.key_hint = label('Space / Enter apply', 'footer')
    panel.key_hint.setToolTip('Shift+R открывает панель у курсора. Перетаскивайте заголовок для перемещения; меню заголовка содержит горячие клавиши.')
    panel.segment_field = NumberField(1, 1000, 4, (3, 4, 6, 8, 12, 16, 24, 32, 48, 64, 128), surface)
    panel.count = panel.segment_field.spin
    panel.count.setToolTip('Количество сегментов выделенной цепочки. Shift+Wheel над видом Maya меняет значение.')
    panel.mode = ComboField(surface);panel.mode.addItems(['N-gons', 'Triangles'])
    panel.mode.setToolTip('N-gons сохраняет соседние грани; Triangles разбивает затронутые N-gons. Совместимые полосы остаются связанными. Alt переключает режим.')
    panel.ngons = QtWidgets.QRadioButton('Keep n-gons', panel)
    panel.connect = QtWidgets.QRadioButton('Triangles', panel)
    panel.ngons.hide();panel.connect.hide()
    (panel.connect if connected else panel.ngons).setChecked(True)
    panel.mode.setCurrentIndex(1 if connected else 0)
    panel.segment_shortcut = label('Shift + Wheel', 'shortcut');panel.segment_shortcut.setAlignment(QtCore.Qt.AlignCenter)
    panel.control_shortcut = label('Ctrl + Shift + Wheel', 'shortcut');panel.control_shortcut.setAlignment(QtCore.Qt.AlignCenter)
    for attribute in ('rule_a', 'rule_b'):
        rule = QtWidgets.QFrame(surface);rule.setObjectName('divider');rule.setFixedHeight(1)
        setattr(panel, attribute, rule)
    for attribute in ('divider_a', 'divider_b'):
        divider = QtWidgets.QFrame(surface);divider.setObjectName('groupDivider');divider.setFixedWidth(1)
        setattr(panel, attribute, divider)

    panel.curve_field = NumberField(3, 32, 4, (3, 4, 6, 8, 12, 16, 24, 32), surface)
    panel.control_points = panel.curve_field.spin
    panel.control_points.setToolTip('Измените или подтвердите значение для создания управляющих кривых. Ctrl+Shift+Wheel также создаёт или перестраивает их с сохранением формы меша.')
    panel.curve_actions = QtWidgets.QWidget(surface)
    controls = QtWidgets.QHBoxLayout(panel.curve_actions);controls.setContentsMargins(0, 0, 0, 0);controls.setSpacing(2)
    panel.reset_curves = QtWidgets.QPushButton('Reset');panel.reset_curves.setObjectName('subtle')
    panel.reset_curves.setToolTip('Вернуть начальные положения точек текущего набора управляющих кривых.')
    panel.remove_curves = QtWidgets.QPushButton('Remove');panel.remove_curves.setObjectName('subtle')
    panel.remove_curves.setToolTip('Удалить управляющие кривые и их деформации, сохранив предпросмотр сегментов.')
    panel.reset_curves.setAutoDefault(False);panel.remove_curves.setAutoDefault(False)
    controls.addWidget(panel.reset_curves);controls.addWidget(panel.remove_curves)
    panel.reset_curves.hide();panel.remove_curves.hide()
    panel.commit = QtWidgets.QWidget(surface)
    actions = QtWidgets.QHBoxLayout(panel.commit);actions.setContentsMargins(0, 0, 0, 0);actions.setSpacing(6)
    panel.apply = ActionButton('Apply');panel.cancel = ActionButton('Cancel')
    panel.apply.setObjectName('primary')
    panel.apply.setToolTip('Сохранить показанный результат: Space или Enter.')
    panel.cancel.setToolTip('Восстановить исходный меш: Esc.')
    actions.addWidget(panel.cancel);actions.addWidget(panel.apply)
    for widget in (panel.apply, panel.cancel):
        widget.setFixedHeight(28)
        widget.setAutoDefault(False)
    panel.feedback = FeedbackLabel(panel);panel.feedback.setObjectName('feedback');panel.feedback.setWordWrap(True)
    panel.surface_layout.addWidget(panel.feedback)
    panel.layout_picker = ComboField(surface);panel.layout_picker.setObjectName('layoutPicker')
    panel.layout_picker.addItems(list(LAYOUTS));panel.layout_picker.setFixedSize(100, 24)
    panel.layout_picker.setToolTip('Компоновка панели. Переключение сохраняет текущий предпросмотр.')
    for index, description in enumerate(LAYOUTS.values()):
        panel.layout_picker.setItemData(index, description, QtCore.Qt.ToolTipRole)
    # Appearance is secondary; the visible selector changes placement, not color.
    panel.style_picker = ComboField(panel)
    panel.style_picker.addItems(list(STYLES));panel.style_picker.setCurrentText(style if style in STYLES else 'Graphite')
    panel.style_picker.hide()
    options = QtWidgets.QMenu(panel.options)
    options.addSection('Viewport shortcuts')
    for text in ('Shift+Wheel: mesh segments', 'Shift+Ctrl+Wheel: curve CVs', 'Tap Alt: toggle topology', 'Space / Enter: apply; Esc: cancel'):
        action = options.addAction(text);action.setEnabled(False)
    panel.select_curve_cvs_action = options.addAction('Select curve CVs')
    panel.select_curve_cvs_action.setEnabled(False)
    layouts = options.addMenu('Layout');panel.layout_actions = {}
    for name in LAYOUTS:
        action = layouts.addAction(name);action.setCheckable(True)
        action.triggered.connect(lambda checked=False, key=name: panel.layout_picker.setCurrentText(key))
        panel.layout_actions[name] = action
    appearance = options.addMenu('Appearance')
    panel.style_actions = {}
    for name in STYLES:
        action = appearance.addAction(name);action.setCheckable(True)
        action.triggered.connect(lambda checked=False, key=name: panel.style_picker.setCurrentText(key))
        panel.style_actions[name] = action
    panel.options.setMenu(options);panel.options.setPopupMode(QtWidgets.QToolButton.InstantPopup)
    panel.header.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
    panel.header.customContextMenuRequested.connect(lambda point: options.popup(panel.header.mapToGlobal(point)))
    panel.title.setToolTip('ReCurve '+version+' | Shift+R\nПКМ по заголовку — настройки.')
    panel.style_picker.currentTextChanged.connect(lambda name: set_style(panel, name))
    panel.layout_picker.currentTextChanged.connect(lambda name: set_layout(panel, name))
    set_style(panel, panel.style_picker.currentText())
    set_layout(panel, layout)
    panel.placement = PanelPlacement(panel)
