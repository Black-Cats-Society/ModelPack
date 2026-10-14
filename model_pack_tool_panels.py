"""Bottom settings panels and editable modifier bindings for Model Pack."""
from PySide6 import QtCore,QtWidgets
import json
from maya import cmds
from maya.api import OpenMaya as om
import model_pack_modifiers as modifiers
import model_pack_mirror_session as mirror_session


EDITABLE={'mirror_cut','mirror_dup','reset_tube','clone','slide_bevel','linear_shift'}


class ToolPanels:
    def _close_edit_panel(self):
        viewport=getattr(self,'_mirror_viewport',None);self._mirror_viewport=None
        if viewport is not None:viewport.close()
        token=getattr(self,'_mirror_session',None)
        kind=self._edit_kind or self._active_tool
        panel=self._edit_panel;nodes=list(self._modifier_nodes);size=self._edit_compact_size
        # Clear ownership before baking: Maya callbacks can run during commit.
        self._mirror_session=None;self._edit_panel=None;self._edit_kind=None;self._modifier_nodes=[]
        self._edit_compact_size=None
        if self._active_tool==kind:self._active_tool=None
        try:
            if token is not None:mirror_session.finish(token)
            if kind=='mirror_dup' and nodes:
                cmds.undoInfo(openChunk=True,chunkName='Apply Mirror Dup')
                try:mirror_session.apply(nodes)
                finally:cmds.undoInfo(closeChunk=True)
        finally:
            if panel is not None:
                panel.hide();self.layout().removeWidget(panel);panel.deleteLater();self.layout().activate()
            if size is not None:self.resize(size)

    def _show_edit_panel(self,kind):
        if self._edit_panel is not None and self._edit_kind==kind:return
        from tube_resection import MirrorMenuWidget,ResectMenuWidget
        self._close_edit_panel();self._active_tool=kind;self._edit_kind=kind;self._edit_compact_size=QtCore.QSize(self.size())
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
            for label,value in (('Равная ширина','uniform'),('Пропорционально','relative')):
                button=QtWidgets.QPushButton(label,panel);button.setCheckable(True);button.setChecked(mode==value)
                button.clicked.connect(lambda checked=False,v=value:self._set_bevel_mode(v) if bevel else self._set_clone_mode(v))
                group.addButton(button);row.addWidget(button);panel.mode_buttons[value]=button
            panel.mode_group=group;layout.addLayout(row)
            grid=QtWidgets.QGridLayout();layout.addLayout(grid)
            grid.addWidget(QtWidgets.QLabel('Количество рёбер',panel),0,0)
            panel.count=QtWidgets.QSpinBox(panel);panel.count.setRange(1,16);panel.count.setValue(self._edge_count(bevel))
            panel.count.setKeyboardTracking(False);grid.addWidget(panel.count,0,1)
            grid.addWidget(QtWidgets.QLabel('Положение доп. рёбер',panel),1,0)
            panel.bias=QtWidgets.QSpinBox(panel);panel.bias.setRange(0,100);panel.bias.setSingleStep(5)
            panel.bias.setSuffix('%');panel.bias.setKeyboardTracking(False);panel.bias.setValue(self._edge_bias(bevel))
            panel.bias.setEnabled(panel.count.value()>1);grid.addWidget(panel.bias,1,1)
            panel.count.valueChanged.connect(lambda value:self._set_edge_count(value,bevel))
            panel.count.valueChanged.connect(lambda value:panel.bias.setEnabled(value>1))
            def bias(value):
                snapped=self._set_edge_bias(value,bevel)
                if value!=snapped:
                    block=QtCore.QSignalBlocker(panel.bias);panel.bias.setValue(snapped)
            panel.bias.valueChanged.connect(bias)
        else:
            from model_pack_linear_shift import ui
            panel.frame_buttons={};panel.axis_buttons={}
            for values,store,selected,callback in (
                ((('Локально','local'),('Глобально','global'),('Мировые','world')),panel.frame_buttons,ui.frame_mode(),ui.set_frame_mode),
                ((('X','x'),('Y','y'),('Z','z')),panel.axis_buttons,ui.plane_axis(),ui.set_plane_axis)):
                row=QtWidgets.QHBoxLayout();group=QtWidgets.QButtonGroup(panel)
                for label,value in values:
                    button=QtWidgets.QPushButton(label,panel);button.setCheckable(True);button.setChecked(value==selected)
                    button.setMinimumHeight(26);group.addButton(button);row.addWidget(button);store[value]=button
                    button.clicked.connect(lambda checked=False,v=value,cb=callback:cb(v))
                layout.addLayout(row)
        if kind in ('clone','slide_bevel','linear_shift'):
            button=QtWidgets.QPushButton('Вернуться к инструменту',panel)
            button.setToolTip('Возвращает управление мышью после Move, Rotate или Scale.')
            button.clicked.connect(lambda checked=False:self._resume_edit_gesture(kind));layout.addWidget(button)
            panel.resume=button
        self.layout().addWidget(panel,self._panel_row,0,1,2);panel.show();self.layout().activate()
        self.resize(self.width(),max(self.height(),self.sizeHint().height()))

    def _activate_modifier(self,kind):
        if self._edit_kind==kind or self._active_tool==kind:
            self._close_edit_panel();self._active_tool=None;self._sync_slide_button();return False
        self._prepare_tool(kind)
        original=om.MGlobal.getActiveSelectionList()
        if kind!='mirror_cut':cmds.undoInfo(openChunk=True,chunkName=kind)
        nodes=[]
        try:
            targets=modifiers.targets()
            if kind=='reset_tube':
                jobs=[]
                for mesh in targets:
                    node=modifiers.find(kind,mesh)
                    if node is None:
                        fn=modifiers._mesh_fn(mesh);seam=[]
                        for index in range(original.length()):
                            path,component=original.getComponent(index)
                            if component.isNull() or component.apiType()!=om.MFn.kMeshEdgeComponent:continue
                            if path.node().hasFn(om.MFn.kTransform):path.extendToShape()
                            if path.fullPathName()==fn.fullPathName():seam.extend(om.MFnSingleIndexedComponent(component).getElements())
                        if not seam:raise ValueError('Reset Tube: выделите продольный шов рёбрами.')
                        modifiers.validate_reset_tube(mesh,seam)
                        jobs.append((mesh,seam,None))
                    else:jobs.append((mesh,None,node))
                for mesh,seam,node in jobs:
                    nodes.append(node or modifiers.reset_tube(mesh,seam,int(cmds.optionVar(q='ModelPack_ResectSides'))))
            else:
                axis=int(cmds.optionVar(q='ModelPack_MirrorAxis'));space=int(cmds.optionVar(q='ModelPack_MirrorSpace'))
                def create_mirrors():
                    from model_pack_mirror_plane import prepare
                    result=[]
                    for mesh in targets:
                        existing=modifiers.find(kind,mesh)
                        if existing is not None:
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
            if token is not None:mirror_session.finish(token);self._mirror_session=None
            cmds.warning(str(error));self._active_tool=None;return False
        finally:
            if kind!='mirror_cut':cmds.undoInfo(closeChunk=True)
        self._read_modifier_options(kind,nodes[0])
        token=getattr(self,'_mirror_session',None)
        self._mirror_session=None
        self._show_edit_panel(kind);self._mirror_session=token
        self._modifier_nodes=nodes;self._sync_slide_button()
        if kind in ('mirror_cut','mirror_dup'):
            from model_pack_mirror_viewport import Viewport
            self._mirror_viewport=Viewport(self,nodes)
            cmds.setToolTo('selectSuperContext')
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
                              ("ModelPack_BevelSecondaryPercent" if bevel else "ModelPack_CloneSecondaryPercent",info['bias_percent']),
                              ("ModelPack_BevelOffsetMode" if bevel else "ModelPack_CloneOffsetMode",int(info['mode']=='uniform'))):
                cmds.optionVar(intValue=(key,value))
            return
        if self._edit_kind not in ('mirror_cut','mirror_dup','reset_tube') or self._edit_panel is None:return
        node=next((n for n in self._modifier_nodes if cmds.objExists(n)),None)
        if node is None:return
        self._read_modifier_options(self._edit_kind,node);widget=self._edit_panel.options
        if self._edit_kind=='reset_tube':
            block=QtCore.QSignalBlocker(widget.spin);widget.spin.setValue(cmds.getAttr(node+'.sides'))
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
            def update():
                for node in nodes:
                    source=modifiers.related(node,'modelPackSource')
                    if kind=='mirror_cut':
                        pivot=cmds.xform(source,q=True,objectSpace=True,rotatePivot=True) if space else (0,0,0)
                        modifiers.mirror_cut(source,axis,space,int(cmds.optionVar(q='ModelPack_MirrorDirection')),
                            float(cmds.optionVar(q='ModelPack_MirrorThreshold')),pivot)
                    elif kind=='mirror_dup':
                        modifiers.mirror_duplicate(source,axis,space)
                    else:cmds.setAttr(node+'.sides',int(cmds.optionVar(q='ModelPack_ResectSides')))
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
