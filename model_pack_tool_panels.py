"""Bottom settings panels and editable modifier bindings for Model Pack."""
from PySide6 import QtCore,QtWidgets
import json
import sys
from maya import cmds
from maya.api import OpenMaya as om
import model_pack_modifiers as modifiers
import model_pack_mirror_session as mirror_session


EDITABLE={'mirror_cut','mirror_dup','reset_tube','clone','slide_bevel','linear_shift','edge_flow','make_planar'}


class SlideBevelWidth(QtWidgets.QWidget):
    def __init__(self, panel):
        super().__init__(panel)
        self.panel = panel
        self._source_key = None
        self._source_plan = None
        self._limit_key = None
        self._scale = 1.0
        self._limit = 0.0
        self._slider_limit = 0.0
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QtWidgets.QLabel('Ширина', self))
        self.slider = QtWidgets.QSlider(QtCore.Qt.Horizontal, self)
        self.slider.setObjectName('slideBevelWidthSlider')
        self.slider.setRange(0, 1000)
        self.slider.setPageStep(50)
        self.slider.setToolTip('Перетащите для предпросмотра ширины. Отпустите, чтобы применить.')
        row.addWidget(self.slider, 1)
        self.spin = QtWidgets.QDoubleSpinBox(self)
        self.spin.setObjectName('slideBevelWidthValue')
        self.spin.setMaximumWidth(86)
        self.spin.setKeyboardTracking(False)
        self.spin.installEventFilter(self)
        self.spin.lineEdit().installEventFilter(self)
        row.addWidget(self.spin)
        self.slider.valueChanged.connect(self._slider_changed)
        self.slider.sliderReleased.connect(self._finish_drag)
        self.spin.valueChanged.connect(self._set_width)
        self.spin.editingFinished.connect(self._finish_text)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(120)
        self.timer.timeout.connect(self.sync)
        self.timer.start()
        self.sync()

    def sync(self):
        from slide_for_maya import bevel, edit_session, ui
        if self.slider.isSliderDown() or (self.spin.hasFocus() and self.spin.lineEdit().isModified()):
            return
        selection = edit_session.selection_key()
        current = ui.bevel_width_state(selection)
        if current is not None:
            plan, mode, amount = current
        else:
            mode = 'relative' if self.panel.mode_buttons['relative'].isChecked() else 'uniform'
            if self._source_key != selection:
                self._source_key = selection
                try:
                    self._source_plan = (bevel.analyze_bevel_selection()
                                         if any('.e[' in item for item in selection) else None)
                except ui.SlideError:
                    self._source_plan = None
            plan, amount = self._source_plan, 0.0
        if self._limit_key is None or self._limit_key[0] is not plan or self._limit_key[1] != mode:
            self._limit_key = plan, mode
            try:
                if plan and plan.has_ngon_turn and mode == 'relative':
                    self._limit = 1.0
                else:
                    self._limit = bevel._actual_amount(plan, sys.float_info.max, mode) if plan else 0.0
            except ui.SlideError:
                self._limit = 0.0
        relative = mode == 'relative'
        self._scale = 100.0 if relative else 1.0 / om.MDistance(1.0, om.MDistance.uiUnit()).asCentimeters()
        limit = max(self._limit, amount)
        self._slider_limit = min(limit * self._scale, 100.0 if relative else 1.0)
        self.setEnabled(limit > 0)
        with QtCore.QSignalBlocker(self.slider), QtCore.QSignalBlocker(self.spin):
            self.spin.setDecimals(6 if 0 < limit * self._scale < 0.01 else 3)
            self.spin.setSingleStep(max(limit * self._scale / 100, 10 ** -self.spin.decimals()))
            self.spin.setRange(0, limit * self._scale)
            self.spin.setSuffix('%' if relative else '')
            self.spin.setToolTip('Ширина в процентах' if relative else
                                 f'Ширина в единицах сцены ({cmds.currentUnit(query=True, linear=True)})')
            self.spin.setValue(amount * self._scale)
            position = min(1.0, amount * self._scale / self._slider_limit) if self._slider_limit else 0
            self.slider.setValue(round(position * self.slider.maximum()))

    def _slider_changed(self, position):
        from slide_for_maya import ui
        value = self._slider_limit * position / self.slider.maximum()
        with QtCore.QSignalBlocker(self.spin):
            self.spin.setValue(value)
        ui.set_bevel_width(value / self._scale, self._limit_key[1],
                           dragging=self.slider.isSliderDown())
        if not self.slider.isSliderDown():
            self.sync()

    def _set_width(self, value):
        from slide_for_maya import ui
        ui.set_bevel_width(value / self._scale, self._limit_key[1])
        self.sync()

    def eventFilter(self, widget, event):
        if (widget in (self.spin, self.spin.lineEdit()) and
                event.type() == QtCore.QEvent.KeyPress and
                event.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter)):
            self.spin.interpretText()
            self._finish_text()
            return True
        return super().eventFilter(widget, event)

    def _finish_text(self):
        self.spin.lineEdit().setModified(False)
        self.sync()

    def _finish_drag(self):
        from slide_for_maya import ui
        ui.finish_bevel_width()
        self.sync()

    def stop(self):
        from slide_for_maya import ui
        self.timer.stop()
        ui.finish_bevel_width(cancel=True)


class EdgeFlowLength(QtWidgets.QWidget):
    """Adjust an equalized selection in scene units, with one undo per drag."""

    def __init__(self, panel):
        super().__init__(panel)
        self.session = None
        self.length = 0.0
        self.slider_limit = 0.0
        self.chunk_open = False
        self.drag_warning = None
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.button = QtWidgets.QPushButton('Equal Length', self)
        self.button.setObjectName('edgeFlowEqualLength')
        self.button.setMinimumHeight(32)
        self.button.setAutoDefault(False)
        self.button.setToolTip('Выровнять длину выбранных рёбер по средней. Затем можно задать общую длину.')
        layout.addWidget(self.button)
        row = QtWidgets.QHBoxLayout()
        layout.addLayout(row)
        row.addWidget(QtWidgets.QLabel('Длина', self))
        self.slider = QtWidgets.QSlider(QtCore.Qt.Horizontal, self)
        self.slider.setObjectName('edgeFlowLengthSlider')
        self.slider.setRange(1, 2000)
        self.slider.setPageStep(50)
        self.slider.setToolTip('Общая длина рёбер. Перетаскивание отменяется одним Ctrl+Z.')
        row.addWidget(self.slider, 1)
        self.spin = QtWidgets.QDoubleSpinBox(self)
        self.spin.setObjectName('edgeFlowLengthValue')
        self.spin.setMaximumWidth(94)
        self.spin.setKeyboardTracking(False)
        row.addWidget(self.spin)
        self.button.clicked.connect(self.equalize)
        self.slider.valueChanged.connect(self._slider_changed)
        self.slider.sliderReleased.connect(self._end_drag)
        self.spin.valueChanged.connect(self._spin_changed)
        self.callbacks = [om.MEventMessage.addEventCallback(event, self.reset)
                          for event in ('SelectionChanged', 'Undo', 'Redo')]
        self.callbacks.extend(om.MSceneMessage.addCallback(event, self.reset)
                              for event in (om.MSceneMessage.kBeforeNew, om.MSceneMessage.kBeforeOpen))
        self.reset()

    @staticmethod
    def _scene_units(length):
        return om.MDistance(length, om.MDistance.kCentimeters).asUnits(om.MDistance.uiUnit())

    def _display(self, length):
        value = self._scene_units(length)
        with QtCore.QSignalBlocker(self.slider), QtCore.QSignalBlocker(self.spin):
            self.spin.setValue(value)
            if self.slider_limit:
                self.slider.setValue(round(length / self.slider_limit * self.slider.maximum()))

    def equalize(self):
        from model_pack_edge_flow import EqualLength
        self.reset()
        try:
            self.session = EqualLength.capture()
            length = self.session.default_length
            self.slider_limit = length * 2.0
            value = self._scene_units(length)
            with QtCore.QSignalBlocker(self.spin):
                self.spin.setDecimals(8 if value < 0.01 else 4)
                self.spin.setRange(10 ** -self.spin.decimals(), value * 1000)
                self.spin.setSingleStep(max(value / 100, self.spin.minimum()))
                self.spin.setSuffix(' ' + cmds.currentUnit(query=True, linear=True))
            self.slider.setEnabled(True)
            self.spin.setEnabled(True)
            self._display(length)
            self._apply_length(length)
        except (ValueError, RuntimeError) as error:
            cmds.warning('Edge Flow: ' + str(error))

    def _apply_length(self, length, dragging=False):
        if self.session is None:
            return
        if not self.chunk_open:
            if not cmds.undoInfo(query=True, state=True):
                raise ValueError('Включите Undo в Maya для изменения длины рёбер.')
            cmds.undoInfo(openChunk=True, chunkName='Edge Flow Equal Length')
            self.chunk_open = True
        try:
            self.session.apply(length)
            self.length = length
            self.slider_limit = max(self.slider_limit, length)
        finally:
            if not dragging:
                self._end_drag()
        self._display(length)

    def _slider_changed(self, position):
        dragging = self.slider.isSliderDown()
        try:
            self._apply_length(position * self.slider_limit / self.slider.maximum(), dragging)
            self.drag_warning = None
        except (ValueError, RuntimeError) as error:
            if dragging:
                self.drag_warning = str(error)
            else:
                cmds.warning('Edge Flow: ' + str(error))
            if self.length:
                self._display(self.length)

    def _spin_changed(self, value):
        try:
            length = om.MDistance(value, om.MDistance.uiUnit()).asCentimeters()
            self._apply_length(length)
        except (ValueError, RuntimeError) as error:
            cmds.warning('Edge Flow: ' + str(error))
            if self.length:
                self._display(self.length)

    def _end_drag(self):
        if self.chunk_open:
            self.chunk_open = False
            cmds.undoInfo(closeChunk=True)
        if self.drag_warning:
            cmds.warning('Edge Flow: ' + self.drag_warning)
            self.drag_warning = None

    def reset(self, *_):
        self._end_drag()
        self.session = None
        self.length = 0.0
        self.slider_limit = 0.0
        self.slider.setEnabled(False)
        self.spin.setEnabled(False)

    def stop(self):
        self.reset()
        for callback in self.callbacks:
            om.MMessage.removeCallback(callback)
        self.callbacks.clear()


class ToolPanels:
    def _close_edit_panel(self, interrupted=False):
        controller=getattr(QtWidgets.QApplication.instance(),'_model_pack_parameter_drag',None)
        if controller is not None:controller.finish()
        gesture=getattr(self,'_tube_gesture',None);self._tube_gesture=None
        if gesture is not None:gesture.close()
        viewport=getattr(self,'_mirror_viewport',None);self._mirror_viewport=None
        if viewport is not None:viewport.close()
        token=getattr(self,'_mirror_session',None)
        reset_group=getattr(self,'_reset_tube_chunk',False);self._reset_tube_chunk=False
        panel_group=getattr(self,'_panel_undo',None);self._panel_undo=None
        kind=self._edit_kind or self._active_tool
        panel=self._edit_panel;nodes=list(self._modifier_nodes);size=self._edit_compact_size
        if panel is not None and hasattr(panel, 'width_control'):
            panel.width_control.stop()
        if panel is not None and hasattr(panel, 'edge_length'):
            panel.edge_length.stop()
        # Clear ownership before baking: Maya callbacks can run during commit.
        self._mirror_session=None;self._edit_panel=None;self._edit_kind=None;self._modifier_nodes=[]
        self._mirror_frames=None
        self._edit_compact_size=None
        if self._active_tool==kind:self._active_tool=None
        try:
            if token is not None:mirror_session.finish(token, freeze=not interrupted)
            if not interrupted and kind=='mirror_dup' and nodes:
                cmds.undoInfo(openChunk=True,chunkName='Apply Mirror Dup')
                try:mirror_session.apply(nodes)
                finally:cmds.undoInfo(closeChunk=True)
            if not interrupted and kind=='reset_tube' and any(cmds.objExists(n) and modifiers.reset_delete_original(n) for n in nodes):
                cmds.undoInfo(openChunk=True,chunkName='Apply Reset Tube')
                try:modifiers.finish_reset_tubes(nodes)
                finally:cmds.undoInfo(closeChunk=True)
        finally:
            if reset_group:cmds.undoInfo(closeChunk=True)
            if panel_group is not None:panel_group.close()
            if kind=='edge_flow' and hasattr(self,'edge_flow_button'):self.edge_flow_button.setChecked(False)
            if panel is not None:
                panel.hide();self.layout().removeWidget(panel);panel.deleteLater();self.layout().activate()
            if size is not None:self.resize(size)

    def _show_edit_panel(self,kind):
        if self._edit_panel is not None and self._edit_kind==kind:return
        from tube_resection import MirrorMenuWidget,ResectMenuWidget
        self._close_edit_panel();self._active_tool=kind;self._edit_kind=kind;self._edit_compact_size=QtCore.QSize(self.size())
        if kind in ('clone','slide_bevel','linear_shift','edge_flow'):
            from model_pack_theme import UndoGroup
            self._panel_undo=UndoGroup({'clone':'Clone Slide','slide_bevel':'Slide Bevel',
                'linear_shift':'Plane Deform','edge_flow':'Edge Flow'}[kind],self._close_edit_panel)
        panel=QtWidgets.QWidget(self);panel.setObjectName('modelPackSettings'+kind)
        self._edit_panel=panel;layout=QtWidgets.QVBoxLayout(panel);layout.setContentsMargins(0,5,0,0)
        if kind in ('mirror_cut','mirror_dup'):
            widget=MirrorMenuWidget(self,panel,is_duplicate=kind=='mirror_dup')
            layout.addWidget(widget);panel.options=widget
        elif kind=='reset_tube':
            widget=ResectMenuWidget(panel,on_change=lambda *_:self._modifier_settings_changed(kind))
            layout.addWidget(widget);panel.options=widget
        elif kind in ('clone','slide_bevel'):
            bevel=kind=='slide_bevel';row=QtWidgets.QHBoxLayout();group=QtWidgets.QButtonGroup(panel)
            mode=self._bevel_mode() if bevel else self._clone_mode()
            panel.mode_buttons={}
            for label,value in (('Uniform Width','uniform'),('Relative','relative')):
                button=QtWidgets.QPushButton(label,panel);button.setCheckable(True);button.setChecked(mode==value)
                button.clicked.connect(lambda checked=False,v=value:self._set_bevel_mode(v) if bevel else self._set_clone_mode(v))
                group.addButton(button);row.addWidget(button);panel.mode_buttons[value]=button
            panel.mode_group=group;layout.addLayout(row)
            grid=QtWidgets.QGridLayout();layout.addLayout(grid)
            grid.addWidget(QtWidgets.QLabel('Количество рёбер',panel),0,0)
            panel.count=QtWidgets.QSpinBox(panel);panel.count.setRange(1,16);panel.count.setValue(self._edge_count(bevel))
            panel.count.setKeyboardTracking(False);grid.addWidget(panel.count,0,1)
            grid.addWidget(QtWidgets.QLabel('Шаг доп. рёбер' if bevel else 'Положение доп. рёбер',panel),1,0)
            panel.bias=QtWidgets.QSpinBox(panel);panel.bias.setRange(0,100);panel.bias.setSingleStep(1 if bevel else 5)
            panel.bias.setSuffix('%');panel.bias.setKeyboardTracking(False);panel.bias.setValue(self._edge_bias(bevel))
            panel.bias.setEnabled(panel.count.value()>1);grid.addWidget(panel.bias,1,1)
            panel.count.valueChanged.connect(lambda value:self._set_edge_count(value,bevel))
            panel.count.valueChanged.connect(lambda value:panel.bias.setEnabled(value>1))
            def bias(value):
                snapped=self._set_edge_bias(value,bevel)
                if value!=snapped:
                    block=QtCore.QSignalBlocker(panel.bias);panel.bias.setValue(snapped)
            panel.bias.valueChanged.connect(bias)
            if bevel:
                panel.count.setToolTip('Количество рядов опорных рёбер с каждой стороны. Исходное ребро остаётся на месте.')
                panel.bias.setToolTip('Шаг дополнительных рёбер в процентах от ширины при подтверждении. Дальше шаг сохраняется при изменении ширины.')
                panel.width_control = SlideBevelWidth(panel)
                layout.addWidget(panel.width_control)
        elif kind=='edge_flow':
            row=QtWidgets.QHBoxLayout();layout.addLayout(row)
            for label,mode,tip in (
                    ('Relax','relax','Релакс вдоль выбранных контуров или полосы поперечных рёбер. Плоскости границ и крайние точки сохраняются. Ctrl+Z — отменить.'),
                    ('Space','space','Равномерный шаг вдоль цепочки или между поперечными рёбрами полосы. Крайние точки сохраняются. Ctrl+Z — отменить.')):
                button=QtWidgets.QPushButton(label,panel);button.setMinimumHeight(32);button.setAutoDefault(False)
                button.setObjectName('edgeFlow'+label);button.setToolTip(tip)
                button.clicked.connect(lambda checked=False,value=mode:self._edge_flow_action(value))
                row.addWidget(button);setattr(panel,mode,button)
            row=QtWidgets.QHBoxLayout();layout.addLayout(row);panel.reference=None
            panel.set_reference=QtWidgets.QPushButton('Set Reference',panel)
            panel.set_reference.setObjectName('edgeFlowSetReference')
            panel.set_reference.setToolTip('Запомнить направление выбранного ребра или нескольких параллельных рёбер. Затем выделите изменяемые рёбра и нажмите Parallel.')
            panel.set_reference.clicked.connect(self._edge_flow_reference)
            panel.parallel=QtWidgets.QPushButton('Parallel',panel);panel.parallel.setObjectName('edgeFlowParallel')
            panel.parallel.setToolTip('Выровнять рёбра по направлению опоры в плоскости граней. На сгибах вершины движутся вдоль стыка. Ctrl+Z — отменить.')
            panel.parallel.clicked.connect(lambda checked=False:self._edge_flow_action('parallel'))
            for button in (panel.set_reference,panel.parallel):
                button.setMinimumHeight(32);button.setAutoDefault(False);row.addWidget(button)
            panel.reference_hint=QtWidgets.QLabel('Reference: not set',panel)
            panel.reference_hint.setAlignment(QtCore.Qt.AlignCenter)
            panel.reference_hint.setStyleSheet('color:#a4adb7;font-size:10px;')
            layout.addWidget(panel.reference_hint)
            panel.edge_length = EdgeFlowLength(panel)
            layout.addWidget(panel.edge_length)
        elif kind=='make_planar':
            self._show_planar_controls(panel,layout)
        else:
            from model_pack_linear_shift import ui
            panel.frame_buttons={};panel.axis_buttons={}
            for values,store,selected,callback in (
                ((('Local','local'),('Global','global'),('World','world')),panel.frame_buttons,ui.frame_mode(),ui.set_frame_mode),
                ((('X','x'),('Y','y'),('Z','z')),panel.axis_buttons,ui.plane_axis(),ui.set_plane_axis)):
                row=QtWidgets.QHBoxLayout();group=QtWidgets.QButtonGroup(panel)
                for label,value in values:
                    button=QtWidgets.QPushButton(label,panel);button.setCheckable(True);button.setChecked(value==selected)
                    button.setMinimumHeight(26);group.addButton(button);row.addWidget(button);store[value]=button
                    button.clicked.connect(lambda checked=False,v=value,cb=callback:cb(v))
                layout.addLayout(row)
        if kind in ('clone','slide_bevel','linear_shift'):
            button=QtWidgets.QPushButton('Resume Tool',panel)
            button.setToolTip('Возвращает управление мышью после Move, Rotate или Scale.')
            button.clicked.connect(lambda checked=False:self._resume_edit_gesture(kind));layout.addWidget(button)
            panel.resume=button
        self.layout().addWidget(panel,self._panel_row,0,1,2);panel.show();self.layout().activate()
        self.resize(self.width(),max(self.height(),self.sizeHint().height()))

    def _show_planar_controls(self,panel,layout):
        panel.reference = None
        panel.mode = QtWidgets.QComboBox(panel)
        panel.mode.setObjectName('makePlanarMode')
        for label, value in (('Auto','auto'),('Keep Contour','contour'),
                             ('Pin','pin'),('Perpendicular','perpendicular')):
            panel.mode.addItem(label,value)
        panel.mode.setToolTip('Авто — по общему наклону. Закрепить — через опорные точки. '
                              'Перпендикулярно — под 90° к опоре с сохранением общей границы. '
                              'Сохранить контур — плоский шейдинг с закреплённой границей. '
                              'Внутренние вершины проецируются, если граница плоская.')
        layout.addWidget(panel.mode)
        row = QtWidgets.QHBoxLayout()
        layout.addLayout(row)
        capture = QtWidgets.QPushButton('Set Reference',panel)
        capture.setObjectName('makePlanarReference')
        capture.setToolTip('Сначала выберите опорные грани или вершины. Затем выделите полигоны для выравнивания.')
        clear = QtWidgets.QPushButton('×',panel)
        clear.setMaximumWidth(32)
        clear.setToolTip('Сбросить опору')
        clear.setEnabled(False)
        for button in (capture,clear):
            button.setAutoDefault(False)
            button.setMinimumHeight(30)
            row.addWidget(button)
        panel.reference_label = QtWidgets.QLabel('Опора не задана',panel)
        layout.addWidget(panel.reference_label)
        apply = QtWidgets.QPushButton('Apply',panel)
        apply.setObjectName('makePlanarApply')
        apply.setMinimumHeight(32)
        apply.setAutoDefault(False)
        apply.clicked.connect(lambda checked=False:self._apply_planar(panel))
        layout.addWidget(apply)

        def capture_reference():
            from model_pack_linear_shift.planar import Reference
            try:
                panel.reference = Reference()
            except (ValueError,RuntimeError) as error:
                cmds.warning(f'Make Planar: {error}')
                return
            panel.reference_label.setText(f'Компонентов опоры: {panel.reference.count}')
            clear.setEnabled(True)
            if panel.mode.currentData() == 'auto':
                panel.mode.setCurrentIndex(panel.mode.findData('pin'))

        def clear_reference():
            panel.reference = None
            panel.reference_label.setText('Опора не задана')
            panel.mode.setCurrentIndex(0)
            clear.setEnabled(False)

        def mode_changed():
            uses_reference = panel.mode.currentData() != 'contour'
            capture.setEnabled(uses_reference)
            clear.setEnabled(uses_reference and panel.reference is not None)
            panel.reference_label.setVisible(uses_reference)

        capture.clicked.connect(capture_reference)
        clear.clicked.connect(clear_reference)
        panel.mode.currentIndexChanged.connect(mode_changed)

    def _apply_planar(self,panel):
        from model_pack_linear_shift.planar import make_planar, preserve_contour
        mode = panel.mode.currentData()
        if mode == 'contour':
            try:
                moved, shaded = preserve_contour()
                message = (f'Вершин выровнено {moved} · Граней обработано {shaded}' if moved or shaded
                           else 'Поверхность уже выровнена.')
                cmds.inViewMessage(statusMessage=message, pos='midCenterTop', fade=True)
                return moved, shaded
            except (ValueError, RuntimeError) as error:
                cmds.warning(f'Make Planar: {error}')
                return
        reference = panel.reference if mode != 'auto' else None
        if mode != 'auto' and reference is None:
            cmds.warning('Make Planar: выделите грани или вершины и нажмите «Запомнить опору».')
            return
        try:
            return make_planar(reference,perpendicular=mode == 'perpendicular')
        except (ValueError,RuntimeError) as error:
            cmds.warning(f'Make Planar: {error}')

    def tool_edge_flow(self):
        if self._edit_kind=='edge_flow':
            self._close_edit_panel();self._sync_slide_button();return
        self._prepare_tool('edge_flow');self._show_edit_panel('edge_flow');self._sync_slide_button()

    def _edge_flow_action(self,mode):
        from model_pack_edge_flow import apply
        self._edit_panel.edge_length.reset()
        try:apply(mode,reference=getattr(self._edit_panel,'reference',None))
        except (ValueError,RuntimeError) as error:cmds.warning('Edge Flow: '+str(error))

    def _edge_flow_reference(self):
        from model_pack_edge_flow import Reference
        try:
            reference=Reference.capture();self._edit_panel.reference=reference
            self._edit_panel.reference_hint.setText('Reference: {} edge{}'.format(reference.count,'' if reference.count==1 else 's'))
        except (ValueError,RuntimeError) as error:cmds.warning('Edge Flow: '+str(error))

    def _activate_modifier(self,kind):
        if self._edit_kind==kind or self._active_tool==kind:
            self._close_edit_panel();self._active_tool=None;self._sync_slide_button();return False
        pivot=None
        if kind in ('mirror_cut','mirror_dup'):
            from model_pack_mirror_plane import manipulator_pivot
            pivot=manipulator_pivot()
        self._prepare_tool(kind)
        original=om.MGlobal.getActiveSelectionList()
        duplicate_group=None
        if kind=='mirror_dup':
            from model_pack_theme import UndoGroup
            duplicate_group=UndoGroup('Mirror Dup',self._close_edit_panel)
        if kind=='reset_tube':cmds.undoInfo(openChunk=True,chunkName='Reset Tube')
        if kind!='mirror_cut':cmds.undoInfo(openChunk=True,chunkName=kind)
        nodes=[];failure=None
        shared_frames=None
        try:
            targets=modifiers.targets()
            if kind in ('mirror_cut','mirror_dup') and len(targets)>1:
                from model_pack_mirror_plane import selection_frames
                sources=[modifiers.related(node,'modelPackSource') if (node:=modifiers.find(kind,mesh)) else mesh
                         for mesh in targets]
                shared_frames=selection_frames(sources,pivot=pivot)
            if kind=='reset_tube':
                jobs=[];automatic=[]
                for mesh in targets:
                    node=modifiers.find(kind,mesh)
                    source=modifiers.related(node,'modelPackSource') if node else mesh
                    if cmds.getAttr(source+'.visibility') and not cmds.getAttr(source+'.visibility',settable=True):
                        raise ValueError('Reset Tube: нельзя скрыть оригинал — его Visibility заблокирована или подключена.')
                    if node is None:
                        fn=modifiers._mesh_fn(mesh);seam=[]
                        for index in range(original.length()):
                            path,component=original.getComponent(index)
                            if component.isNull() or component.apiType()!=om.MFn.kMeshEdgeComponent:continue
                            if path.node().hasFn(om.MFn.kTransform):path.extendToShape()
                            if path.fullPathName()==fn.fullPathName():seam.extend(om.MFnSingleIndexedComponent(component).getElements())
                        if not seam:
                            from model_pack_tube import automatic_seam
                            seam,method=automatic_seam(mesh)
                            automatic.append((mesh,seam,method))
                        modifiers.validate_reset_tube(mesh,seam)
                        jobs.append((mesh,seam,None))
                    else:jobs.append((mesh,None,node))
                for mesh,seam,node in jobs:
                    nodes.append(node or modifiers.reset_tube(mesh,seam,int(cmds.optionVar(q='ModelPack_ResectSides'))))
                for node in nodes:modifiers.hide_reset_source(node)
                for node in nodes:
                    if not cmds.objExists(node+'.lengthSegments'):
                        cmds.addAttr(node,ln='lengthSegments',at='long',min=0,max=1024,dv=0,keyable=True)
                    for name,attr_kind,default,maximum in (('reductionAngle','double',0.,180.),('extraLoops','long',0,1024),('reduceByAngle','bool',True,1)):
                        if not cmds.objExists(node+'.'+name):
                            cmds.addAttr(node,ln=name,at=attr_kind,min=0,max=maximum,dv=default,keyable=True)
                    cmds.dgdirty(node)
            else:
                axis=int(cmds.optionVar(q='ModelPack_MirrorAxis'));space=int(cmds.optionVar(q='ModelPack_MirrorSpace'))
                def create_mirrors():
                    from model_pack_mirror_plane import prepare,write_many
                    result=[]
                    for mesh in targets:
                        existing=modifiers.find(kind,mesh)
                        if existing is not None and shared_frames is None:
                            if kind=='mirror_cut' and cmds.nodeType(existing)=='modelPackMirrorCut':
                                saved={name:cmds.getAttr(existing+'.'+name) for name in ('axis','space','direction','threshold')}
                                pivot=cmds.xform(mesh,q=True,objectSpace=True,rotatePivot=True) if saved['space'] else (0,0,0)
                                modifiers.mirror_cut(mesh,saved['axis'],saved['space'],saved['direction'],saved['threshold'],pivot)
                            prepare(existing);result.append(existing);continue
                        if kind=='mirror_dup':result.append(modifiers.mirror_duplicate(mesh,axis,space))
                        else:
                            pivot=cmds.xform(mesh,q=True,objectSpace=True,rotatePivot=True) if space else (0,0,0)
                            result.append(modifiers.mirror_cut(mesh,axis,space,int(cmds.optionVar(q='ModelPack_MirrorDirection')),
                                         float(cmds.optionVar(q='ModelPack_MirrorThreshold')),pivot))
                    for node in result:prepare(node)
                    if shared_frames is not None:write_many(result,shared_frames[space])
                    if kind=='mirror_cut':
                        from model_pack_mirror_plane import automatic_direction
                        automatic_direction(result)
                    return result
                if kind=='mirror_cut':self._mirror_session,nodes=mirror_session.start(create_mirrors)
                else:nodes=create_mirrors()
                for mesh in targets:modifiers._mesh_fn(mesh).getPoints()
            if kind!='mirror_cut':cmds.select([modifiers.related(n) for n in nodes],r=True)
            else:
                selection=om.MSelectionList()
                for target in targets:selection.add(target)
                om.MGlobal.setActiveSelectionList(selection)
        except (ValueError,RuntimeError) as error:
            token=getattr(self,'_mirror_session',None)
            if token is not None:mirror_session.finish(token, freeze=not interrupted);self._mirror_session=None
            cmds.warning(str(error));self._active_tool=None
            om.MGlobal.setActiveSelectionList(original)
            failure=str(error)
        finally:
            if kind!='mirror_cut':cmds.undoInfo(closeChunk=True)
        if failure is not None:
            if duplicate_group is not None:duplicate_group.close()
            if kind=='reset_tube':cmds.undoInfo(closeChunk=True)
            if kind=='reset_tube':QtWidgets.QMessageBox.warning(self,'Reset Tube',failure)
            return False
        self._read_modifier_options(kind,nodes[0])
        token=getattr(self,'_mirror_session',None)
        self._mirror_session=None
        self._show_edit_panel(kind);self._mirror_session=token
        self._reset_tube_chunk=kind=='reset_tube'
        if duplicate_group is not None:self._panel_undo=duplicate_group
        self._modifier_nodes=nodes;self._sync_slide_button()
        self._mirror_frames=shared_frames
        if kind in ('mirror_cut','mirror_dup'):
            from model_pack_mirror_viewport import Viewport
            self._mirror_viewport=Viewport(self,nodes)
            cmds.setToolTo('selectSuperContext')
        elif kind=='reset_tube':
            from model_pack_tube_viewport import TubeGesture
            self._tube_gesture=TubeGesture(self)
            self._restore_modifier_controls()
            if automatic:
                self._edit_panel.options.setToolTip('Продольный шов выбран автоматически: '+
                    ('по разрезу UV.' if all(method=='uv' for _,_,method in automatic) else 'по топологии меша.'))
        if not cmds.about(batch=True):cmds.refresh(force=True,currentView=True)
        return True

    def _read_modifier_options(self,kind,node):
        if kind=='reset_tube':
            cmds.optionVar(intValue=('ModelPack_ResectSides',cmds.getAttr(node+'.sides')));return
        if kind=='mirror_dup':
            fields={'Axis':'modelPackAxis','Space':'modelPackSpace'}
        elif cmds.nodeType(node)=='modelPackMirrorCut':
            fields={'Axis':'axis','Space':'space','Direction':'direction','Threshold':'threshold'}
        else:
            fields={'Axis':'axis','Direction':'axisDirection','Threshold':'mergeThreshold'}
            cmds.optionVar(intValue=('ModelPack_MirrorSpace',int(cmds.getAttr(node+'.mirrorAxis')==1)))
        for label,attr in fields.items():
            value=cmds.getAttr(node+'.'+attr)
            if label=='Threshold':cmds.optionVar(floatValue=('ModelPack_Mirror'+label,value))
            else:cmds.optionVar(intValue=('ModelPack_Mirror'+label,value))

    def _restore_modifier_controls(self,*_):
        token=getattr(self,'_mirror_session',None)
        if self._edit_kind=='mirror_cut' and token is not None and token not in mirror_session.ACTIVE:
            self._close_edit_panel();self._active_tool=None;self._sync_slide_button();return
        if self._edit_kind in ('clone','slide_bevel') and self._edit_panel is not None:
            from slide_for_maya import ui
            session=ui._SESSION
            if session is None or not cmds.objExists(session['result_node']):return
            info=json.loads(cmds.getAttr(session['result_node']+'.slideState'))
            panel=self._edit_panel;bevel=self._edit_kind=='slide_bevel'
            for widget,key in ((panel.count,'count'),(panel.bias,'bias_percent')):
                block=QtCore.QSignalBlocker(widget);widget.setValue(info[key])
            panel.bias.setEnabled(info['count']>1)
            panel.mode_buttons[info['mode']].setChecked(True)
            for key,value in (("ModelPack_BevelEdgeCount" if bevel else "ModelPack_CloneEdgeCount",info['count']),
                              ("ModelPack_BevelOuterSpacingPercent" if bevel else "ModelPack_CloneSecondaryPercent",info['bias_percent']),
                              ("ModelPack_BevelOffsetMode" if bevel else "ModelPack_CloneOffsetMode",int(info['mode']=='uniform'))):
                cmds.optionVar(intValue=(key,value))
            return
        if self._edit_kind not in ('mirror_cut','mirror_dup','reset_tube') or self._edit_panel is None:return
        node=next((n for n in self._modifier_nodes if cmds.objExists(n)),None)
        if node is None:return
        self._read_modifier_options(self._edit_kind,node);widget=self._edit_panel.options
        if self._edit_kind=='reset_tube':
            block=QtCore.QSignalBlocker(widget.spin);widget.spin.setValue(cmds.getAttr(node+'.sides'))
            for field,attr in ((widget.angle_spin,'reductionAngle'),(widget.extra_spin,'extraLoops'),(widget.reduce_loops,'reduceByAngle')):
                value=cmds.getAttr(node+'.'+attr);blocker=QtCore.QSignalBlocker(field)
                field.setChecked(value) if isinstance(field,QtWidgets.QCheckBox) else field.setValue(value)
            widget.angle_spin.setEnabled(widget.reduce_loops.isChecked())
            block_flag=QtCore.QSignalBlocker(widget.delete_original)
            widget.delete_original.setChecked(modifiers.reset_delete_original(node))
        else:
            widget._axis=cmds.optionVar(q='ModelPack_MirrorAxis')
            widget._direction=cmds.optionVar(q='ModelPack_MirrorDirection')
            widget.bg_axis.button(widget._axis).setChecked(True)
            widget.bg_space.button(cmds.optionVar(q='ModelPack_MirrorSpace')).setChecked(True)
            widget._sync_axis_labels()
            if hasattr(widget,'threshold'):
                block=QtCore.QSignalBlocker(widget.threshold);widget.threshold.setValue(cmds.optionVar(q='ModelPack_MirrorThreshold'))

    def _modifier_settings_changed(self,kind):
        if self._edit_kind!=kind:return
        viewport=getattr(self,'_mirror_viewport',None)
        if viewport is not None:viewport.release();viewport.hide_angle()
        nodes=[n for n in self._modifier_nodes if cmds.objExists(n)]
        if not nodes:return
        original=om.MGlobal.getActiveSelectionList()
        cmds.undoInfo(openChunk=True,chunkName='Model Pack settings')
        try:
            axis=int(cmds.optionVar(q='ModelPack_MirrorAxis'));space=int(cmds.optionVar(q='ModelPack_MirrorSpace'))
            frames=getattr(self,'_mirror_frames',None)
            if kind in ('mirror_cut','mirror_dup') and frames is not None:
                from model_pack_mirror_plane import fields,frame,write_many
                pose=frame(nodes[0]) if cmds.getAttr(nodes[0]+'.'+fields(nodes[0])[1])==space else frames[space]
            reorient = kind=='mirror_cut' and any(cmds.getAttr(n+'.axis')!=axis or cmds.getAttr(n+'.space')!=space for n in nodes)
            def update():
                for node in nodes:
                    source=modifiers.related(node,'modelPackSource')
                    if kind=='mirror_cut':
                        pivot=cmds.xform(source,q=True,objectSpace=True,rotatePivot=True) if space else (0,0,0)
                        modifiers.mirror_cut(source,axis,space,int(cmds.optionVar(q='ModelPack_MirrorDirection')),
                            float(cmds.optionVar(q='ModelPack_MirrorThreshold')),pivot)
                    elif kind=='mirror_dup':
                        modifiers.mirror_duplicate(source,axis,space)
                    else:
                        sides=int(cmds.optionVar(q='ModelPack_ResectSides'))
                        if cmds.getAttr(node+'.sides')!=sides:cmds.setAttr(node+'.sides',sides)
                        options=self._edit_panel.options
                        for attr,value in (('reductionAngle',options.angle_spin.value()),('extraLoops',options.extra_spin.value()),('reduceByAngle',options.reduce_loops.isChecked())):
                            if cmds.getAttr(node+'.'+attr)!=value:cmds.setAttr(node+'.'+attr,value)
                        modifiers.reset_delete_original(node,self._edit_panel.options.delete_original.isChecked())
                if kind in ('mirror_cut','mirror_dup') and frames is not None:write_many(nodes,pose)
                if reorient:
                    from model_pack_mirror_plane import automatic_direction
                    value=automatic_direction(nodes)
                    if value is not None:
                        cmds.optionVar(intValue=('ModelPack_MirrorDirection',value))
                        widget=self._edit_panel.options;widget._direction=value;widget._sync_axis_labels()
            if kind=='mirror_cut' and getattr(self,'_mirror_session',None) is not None:
                if not mirror_session.update(self._mirror_session,update):
                    raise RuntimeError('Mirror Cut: сеанс завершён. Включите инструмент заново.')
            else:update()
            if not cmds.about(batch=True):cmds.refresh(currentView=True)
        except (ValueError,RuntimeError) as error:cmds.warning(str(error))
        finally:
            cmds.undoInfo(closeChunk=True);om.MGlobal.setActiveSelectionList(original)

    def _resume_edit_gesture(self,kind):
        from slide_for_maya import ui as slide
        from model_pack_linear_shift import ui as plane
        if {'clone':slide.active,'slide_bevel':slide.active_bevel,'linear_shift':plane.active}[kind]():return
        if kind=='clone':slide.activate_drag(mode=self._clone_mode(),count=self._edge_count())
        elif kind=='slide_bevel':slide.activate_bevel_drag(mode=self._bevel_mode(),count=self._edge_count(True))
        elif not plane.active():plane.activate()
        self._sync_slide_button()
