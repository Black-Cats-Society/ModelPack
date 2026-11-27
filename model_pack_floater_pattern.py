"""Floater Pattern panel with live masks and disposable instance previews."""
import uuid
from maya import cmds
from maya.api import OpenMaya as om
from PySide6 import QtCore,QtWidgets
from shiboken6 import isValid
from model_pack_bevel.session import without_undo
from model_pack_floater_pattern_geometry import Surface,curve_polygon,placements

_HOST = None


def shutdown():
    global _HOST
    host,_HOST = _HOST,None
    if host is not None and isValid(host):
        host.stop()


def sync(window):
    window.floater_pattern_button.setChecked(_HOST is not None and _HOST.window is window)


def toggle(window):
    global _HOST
    if _HOST is not None and _HOST.window is window:
        shutdown()
        return
    shutdown()
    window._prepare_tool('floater_pattern')
    _HOST = Host(window)
    _HOST.start()


class Item:
    def __init__(self,path):
        self.handle = om.MObjectHandle(path.node())
        self.dag = om.MDagPath(path)

    def path(self):
        if not self.handle.isValid() or not self.handle.isAlive():
            raise ValueError('Один из объектов удалён. Укажите его заново.')
        return om.MDagPath(self.dag) if self.dag.isValid() else om.MDagPath.getAPathTo(self.handle.object())

    def name(self):
        return self.path().fullPathName()


def capture(kind, many=False):
    selection = om.MGlobal.getActiveSelectionList()
    result = []
    for i in range(selection.length()):
        path = selection.getDagPath(i)
        if path.node().hasFn(om.MFn.kTransform):
            shapes = cmds.listRelatives(path.fullPathName(),shapes=True,noIntermediate=True,fullPath=True) or []
            if len(shapes)!=1:
                raise ValueError('Выберите объект с одной видимой формой.')
            path = om.MSelectionList().add(shapes[0]).getDagPath(0)
        if not path.node().hasFn(kind):
            raise ValueError('Выбран неверный тип объекта.')
        if not any(item.handle.object()==path.node() for item in result):
            result.append(Item(path))
    if not result or (not many and len(result)!=1):
        raise ValueError('Выберите один объект.' if not many else 'Выберите кривые исключения.')
    return result if many else result[0]


class Host(QtCore.QObject):
    def __init__(self,window):
        super().__init__(window)
        self.window = window
        self.source = self.surface = self.boundary = None
        self.exclusions = []
        self.guides = []
        self.faces = None
        self.preview = None
        self.preview_nodes = []
        self.preview_source = None
        self.matrices = []
        self.signature = None
        self.surface_key = None
        self.surface_cache = None
        self.dirty = True
        self.callbacks = []
        self.compact_size = QtCore.QSize(window.size())
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(80)
        self.timer.timeout.connect(self.poll)

    def start(self):
        self.panel = QtWidgets.QGroupBox('Floater Pattern',self.window)
        layout = QtWidgets.QVBoxLayout(self.panel)
        self.labels = {}
        for key,label in (('source','Source'),('surface','Surface / Faces'),('boundary','Boundary'),('exclusions','Exclusions'),('guides','Guides')):
            row = QtWidgets.QHBoxLayout()
            button = QtWidgets.QPushButton(label,self.panel)
            button.setMaximumWidth(102)
            button.setAutoDefault(False)
            button.clicked.connect(lambda checked=False,k=key:self.pick(k))
            row.addWidget(button)
            clear = QtWidgets.QPushButton('×',self.panel)
            clear.setFixedWidth(26);clear.setAutoDefault(False)
            clear.clicked.connect(lambda checked=False,k=key:self.clear(k))
            row.addWidget(clear)
            status = QtWidgets.QLabel('—',self.panel)
            status.setMinimumWidth(0)
            status.setSizePolicy(QtWidgets.QSizePolicy.Ignored,QtWidgets.QSizePolicy.Preferred)
            row.insertWidget(1,status,1)
            layout.addLayout(row)
            self.labels[key] = status
        form = QtWidgets.QFormLayout()
        self.mode = QtWidgets.QComboBox(self.panel)
        self.mode.addItems(['Grid','Staggered'])
        self.mode.setCurrentIndex(1)
        self.mode.setToolTip('Staggered — мозаичная решётка со смещёнными рядами. Grid — прямоугольная решётка.')
        self.mode.currentIndexChanged.connect(self.invalidate)
        form.addRow('Layout',self.mode)
        self.flow = QtWidgets.QComboBox(self.panel)
        self.flow.addItem('Guide Waves','waves')
        self.flow.addItem('Mosaic','mosaic')
        self.flow.setToolTip('Guide Waves — параллельные ряды от гайда внутрь к центру и наружу до границы. Mosaic — общая мозаичная решётка.')
        self.flow.currentIndexChanged.connect(self.invalidate)
        form.addRow('Guide Flow',self.flow)
        self.fields = {}
        for key,label,value in (('columns','Max per Row',30),('rows','Max Rows',15),('angle','Rotation',0.),('margin','Edge Margin',.05),('offset','Surface Offset',.01)):
            if key in ('columns','rows'):
                field = QtWidgets.QSpinBox(self.panel)
                field.setRange(3 if key=='columns' else 2,200)
                field.setToolTip('Максимум флоатеров в самом длинном ряду. На коротких рядах количество уменьшается равномерно.' if key=='columns' else 'Максимум рядов от центра к внешней границе с Guides, по высоте области без Guides. Плотность ограничена размером Source.')
            else:
                field = QtWidgets.QDoubleSpinBox(self.panel)
                field.setDecimals(3)
                field.setRange(-360 if key=='angle' else 0,360 if key=='angle' else 1e6)
            field.setKeyboardTracking(False)
            field.setValue(value)
            field.valueChanged.connect(self.invalidate)
            form.addRow(label,field)
            self.fields[key] = field
        self.output = QtWidgets.QComboBox(self.panel)
        self.output.addItems(['Instances','Copies'])
        form.addRow('Create as',self.output)
        layout.addLayout(form)
        self.live = QtWidgets.QCheckBox('Live Preview',self.panel)
        self.live.setChecked(True);self.live.toggled.connect(self.invalidate)
        layout.addWidget(self.live)
        self.status = QtWidgets.QLabel('Set Source and Surface. Boundary is optional for selected faces.',self.panel)
        self.status.setWordWrap(True);layout.addWidget(self.status)
        button = QtWidgets.QPushButton('Create',self.panel)
        button.setAutoDefault(False);button.clicked.connect(self.create)
        layout.addWidget(button)
        self.window.layout().addWidget(self.panel,self.window._panel_row,0,1,2)
        self.window.floater_pattern_button.setChecked(True)
        self.window.layout().activate()
        self.window.resize(self.window.width(),self.window.sizeHint().height())
        self.callbacks = [om.MSceneMessage.addCallback(event,lambda *_:shutdown())
                          for event in (om.MSceneMessage.kBeforeNew,om.MSceneMessage.kBeforeOpen)]
        self.timer.start()

    def invalidate(self,*_):
        self.dirty = True

    def clear(self,key):
        setattr(self,key,[] if key in ('exclusions','guides') else None)
        if key=='surface':
            self.faces = None
        self.labels[key].setText('—')
        self.invalidate()

    def pick(self,key):
        try:
            kind = om.MFn.kNurbsCurve if key in ('boundary','exclusions','guides') else om.MFn.kMesh
            item = capture(kind,key in ('exclusions','guides'))
            if key=='surface':
                faces = set()
                selection = om.MGlobal.getActiveSelectionList()
                for i in range(selection.length()):
                    path,component = selection.getComponent(i)
                    if not component.isNull():
                        if not component.hasFn(om.MFn.kMeshPolygonComponent):
                            raise ValueError('Surface: выберите меш или его полигоны.')
                        faces.update(om.MFnSingleIndexedComponent(component).getElements())
                captured_faces = sorted(faces) if faces else None
            if key=='source':
                if self.surface and item.handle.object()==self.surface.handle.object():
                    raise ValueError('Source и Surface должны быть разными мешами.')
            if key=='surface' and self.source and item.handle.object()==self.source.handle.object():
                raise ValueError('Source и Surface должны быть разными мешами.')
            setattr(self,key,item)
            if key=='surface':
                self.faces = captured_faces
            names = [x.name().split('|')[-1] for x in item] if isinstance(item,list) else [item.name().split('|')[-1]]
            text = ', '.join(names)+(f' · {len(self.faces)} faces' if key=='surface' and self.faces else '')
            self.labels[key].setText(text)
            self.labels[key].setToolTip(text)
            self.signature = None
            self.invalidate()
        except Exception as error:
            self.status.setText(str(error))

    def fingerprint(self):
        result = []
        for item in [self.source,self.surface,self.boundary]+self.exclusions+self.guides:
            if item is None:
                continue
            path = item.path()
            result.append((path.fullPathName(),tuple(path.inclusiveMatrix())))
            if path.node().hasFn(om.MFn.kNurbsCurve):
                fn = om.MFnNurbsCurve(path)
                result.append((fn.degree,fn.form,tuple(fn.knots()),tuple(tuple(p) for p in fn.cvPositions())))
            else:
                fn = om.MFnMesh(path)
                result.append((tuple(tuple(p) for p in fn.getPoints()),tuple(tuple(a) for a in fn.getVertices())))
        return result

    def calculate(self):
        if self.source is None or self.surface is None:
            raise ValueError('Укажите Source и Surface.')
        surface_fn = om.MFnMesh(self.surface.path())
        key = (tuple(surface_fn.dagPath().inclusiveMatrix()),
               tuple(tuple(p) for p in surface_fn.getPoints()),
               tuple(tuple(a) for a in surface_fn.getVertices()),
               None if self.faces is None else tuple(self.faces))
        if key!=self.surface_key:
            surface = Surface(surface_fn,self.faces)
            self.surface_cache,self.surface_key = surface,key
        else:
            surface = self.surface_cache
        scale = om.MDistance(1.,om.MDistance.uiUnit()).asCentimeters()
        values = {key:self.fields[key].value()*(1 if key=='angle' else scale) for key in ('angle','margin','offset')}
        columns,rows = self.fields['columns'].value(),self.fields['rows'].value()
        values.update(pitch_x=max(1e-8,(surface.high[0]-surface.low[0])/(columns-1)),
                      pitch_y=max(1e-8,(surface.high[1]-surface.low[1])/(rows-1)))
        tolerance = min(values['pitch_x'],values['pitch_y'])*.01
        boundary = curve_polygon(om.MFnNurbsCurve(self.boundary.path()),surface,tolerance) if self.boundary else None
        exclusions = [curve_polygon(om.MFnNurbsCurve(item.path()),surface,tolerance) for item in self.exclusions]
        guides = [curve_polygon(om.MFnNurbsCurve(item.path()),surface,tolerance) for item in self.guides]
        return placements(om.MFnMesh(self.source.path()),surface,boundary,exclusions,
                          guides=guides,row_count=rows,per_row=columns,flow=self.flow.currentData(),
                          staggered=self.mode.currentText()=='Staggered',**values)

    def remove_preview(self):
        if self.preview and cmds.objExists(self.preview):
            with without_undo():
                cmds.delete(self.preview)
        self.preview = None
        self.preview_nodes = []
        self.preview_source = None

    def update_preview(self, matrices):
        source = self.source.handle.hashCode()
        if self.preview_source!=source or not self.preview or not cmds.objExists(self.preview):
            self.remove_preview()
            self.preview = self.build(matrices,True,True)
            self.preview_nodes = cmds.listRelatives(self.preview,children=True,type='transform',fullPath=True) or []
            self.preview_source = source
            return
        count = len(matrices)
        if len(self.preview_nodes)>count:
            cmds.delete(self.preview_nodes[count:])
            self.preview_nodes = self.preview_nodes[:count]
        path = self.source.path()
        path.pop()
        while len(self.preview_nodes)<count:
            node = cmds.instance(path.fullPathName(),name='patternFloater#')[0]
            node = cmds.parent(node,self.preview)[0]
            cmds.setAttr(node+'.overrideEnabled',True)
            cmds.setAttr(node+'.overrideDisplayType',2)
            self.preview_nodes.append(cmds.ls(node,long=True)[0])
        for i,(node,matrix) in enumerate(zip(self.preview_nodes,matrices)):
            if i>=len(self.matrices) or tuple(matrix)!=tuple(self.matrices[i]):
                cmds.xform(node,worldSpace=True,matrix=list(matrix))

    def build(self,matrices,instances,preview):
        source = self.source.path()
        source.pop()
        group = cmds.createNode('transform',name='floaterPatternPreview#' if preview else 'floaterPattern#')
        unparented = None
        try:
            for matrix in matrices:
                node = (cmds.instance(source.fullPathName(),name='patternFloater#')[0] if instances else
                        cmds.duplicate(source.fullPathName(),returnRootsOnly=True,name='patternFloater#')[0])
                unparented = node
                node = cmds.parent(node,group)[0]
                unparented = None
                cmds.xform(node,worldSpace=True,matrix=list(matrix))
                if preview:
                    # Per-instance transform override leaves source shape untouched.
                    cmds.setAttr(node+'.overrideEnabled',True)
                    cmds.setAttr(node+'.overrideDisplayType',2)
            return group
        except Exception:
            if unparented and cmds.objExists(unparented):
                cmds.delete(unparented)
            cmds.delete(group)
            raise

    def poll(self):
        try:
            if not self.live.isChecked():
                self.remove_preview()
                return
            signature = self.fingerprint()
            if not self.dirty and signature==self.signature:
                return
            self.signature = signature
            self.dirty = False
            matrices = self.calculate()
            selection = om.MGlobal.getActiveSelectionList()
            try:
                with without_undo():
                    self.update_preview(matrices)
            finally:
                with without_undo():
                    om.MGlobal.setActiveSelectionList(selection)
            self.matrices = matrices
            self.status.setText(f'{len(matrices)} floaters · Live Preview')
        except Exception as error:
            self.remove_preview()
            self.matrices = []
            self.status.setText(str(error))

    def create(self):
        try:
            matrices = self.calculate()
            if not matrices:
                raise ValueError('В области нет места для флоатера с текущим шагом и отступом.')
            self.remove_preview()
            chunk = 'Floater Pattern '+uuid.uuid4().hex
            cmds.undoInfo(openChunk=True,chunkName=chunk)
            try:
                group = self.build(matrices,self.output.currentIndex()==0,False)
                cmds.select(group,replace=True)
            except Exception:
                cmds.undoInfo(closeChunk=True)
                if cmds.undoInfo(query=True,undoName=True)==chunk:
                    cmds.undo()
                raise
            else:
                cmds.undoInfo(closeChunk=True)
            self.live.setChecked(False)
            self.status.setText(f'Created {len(matrices)} floaters')
        except Exception as error:
            self.status.setText(str(error))

    def stop(self):
        self.timer.stop()
        self.remove_preview()
        for callback in self.callbacks:
            om.MMessage.removeCallback(callback)
        self.callbacks = []
        if isValid(self.window):
            self.panel.hide()
            self.window.layout().removeWidget(self.panel)
            self.window.floater_pattern_button.setChecked(False)
            if self.window._active_tool=='floater_pattern':
                self.window._active_tool = None
            self.window.layout().activate()
            self.window.resize(self.compact_size)
        self.panel.deleteLater()
        self.deleteLater()
