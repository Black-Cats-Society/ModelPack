"""Curve-driven recessed floaters and the standard Model Pack settings panel."""
import importlib
import math
from pathlib import Path
import weakref
from maya import cmds
from maya.api import OpenMaya as om,OpenMayaUI as omui
from PySide6 import QtCore,QtGui,QtWidgets
from shiboken6 import isValid
from model_pack_offset import transient,display_scale
from model_pack_paths import plugin_path
from model_pack_floater_geometry import sample,frame,build,OffsetError
from model_pack_floater_surface import Surface as Projector,world_mesh,build as build_surface

TYPE='modelPackFloater'
PLUGIN=plugin_path(Path(__file__).with_name('model_pack_floater_node.py'))
SURFACE_TYPE='modelPackFloaterSurface'
SURFACE_PLUGIN=plugin_path(Path(__file__).with_name('model_pack_floater_surface_node.py'))
FIELDS=('openingWidth','height','skirtLength')
_HOST=None
_REFRESHED=False


def refresh_plugin():
    global _REFRESHED
    for plugin,kind,module_name,class_name in ((PLUGIN,TYPE,'model_pack_floater_node','FloaterNode'),
                                              (SURFACE_PLUGIN,SURFACE_TYPE,'model_pack_floater_surface_node','SurfaceNode')):
        if not cmds.pluginInfo(plugin,q=True,loaded=True):continue
        from model_pack_mesh_safety import ensure_cache_type,ensure_direction_type
        ensure_cache_type(kind);ensure_direction_type(kind)
        module=importlib.import_module(module_name);importlib.reload(module)
        with transient():
            probe=cmds.createNode(kind,skipSelect=True)
            try:
                cls=type(om.MFnDependencyNode(om.MSelectionList().add(probe).getDependNode(0)).userNode())
                cls.compute=getattr(module,class_name).compute
                cls.setDependentsDirty=getattr(module,class_name).setDependentsDirty
                for node in cmds.ls(type=kind) or []:
                    if node!=probe:
                        # Prime a newly added persistent cache from an existing
                        # session's last-good plan without touching its inputs.
                        fn=om.MFnDependencyNode(om.MSelectionList().add(node).getDependNode(0))
                        plan=getattr(fn.userNode(),'last_plan',None)
                        if plan is not None:
                            from model_pack_mesh_safety import NAME,owned_copy
                            from model_pack_floater_geometry import mesh as plan_mesh
                            fn.findPlug(NAME,False).setMObject(owned_copy(plan_mesh(plan)))
                        if kind==SURFACE_TYPE:
                            parents=cmds.listConnections(node+'.openingWidth',s=True,d=False) or []
                            if parents and cmds.nodeType(parents[0])==TYPE and not cmds.connectionInfo(node+'.flipDirection',isDestination=True):
                                cmds.connectAttr(parents[0]+'.flipDirection',node+'.flipDirection')
                        cmds.dgdirty(node)
            finally:cmds.delete(probe)
    _REFRESHED=True


def ensure_plugin(surface=False):
    global _REFRESHED
    for plugin in (PLUGIN,SURFACE_PLUGIN) if surface else (PLUGIN,):
        if not cmds.pluginInfo(plugin,q=True,loaded=True):cmds.loadPlugin(plugin,quiet=True);_REFRESHED=False
    if not _REFRESHED:refresh_plugin()


def values(node):return tuple(cmds.getAttr(node+'.'+name)/display_scale() for name in FIELDS)


def set_values(node,parameters):
    for name,value in zip(FIELDS,parameters):cmds.setAttr(node+'.'+name,value*display_scale())


class Source:
    def __init__(self,path,node=None):
        self.handle=om.MObjectHandle(path.node());self.instance=path.instanceNumber();self.existing=node
        self.hint=None;self.surface=None;self.flip=False
    def path(self):
        if not self.handle.isValid() or not self.handle.isAlive():raise ValueError('Floater: исходная кривая удалена.')
        paths=om.MDagPath.getAllPathsTo(self.handle.object())
        return next((p for p in paths if p.instanceNumber()==self.instance),paths[0])
    def points(self,width):
        path=self.path();return sample(om.MFnNurbsCurve(path),path.inclusiveMatrix(),width)
    def normal(self,width):
        if self.hint is not None:return self.hint
        path=self.path();matrix=path.inclusiveMatrix();points,_=self.points(width)
        hint=om.MVector(0,1,0).transformAsNormal(matrix)
        origin,_,_,normal,_=frame(points,hint,strict=False)
        try:
            camera=omui.M3dView.active3dView().getCamera()
            if normal*(om.MPoint()*camera.inclusiveMatrix()-origin)<0:normal=-normal
        except (RuntimeError,ValueError):pass
        self.hint=tuple(normal.transformAsNormal(matrix.inverse()).normal())
        return self.hint


class Support:
    def __init__(self,path):self.handle=om.MObjectHandle(path.node());self.instance=path.instanceNumber()
    def path(self):
        if not self.handle.isValid() or not self.handle.isAlive():raise ValueError('Floater: опорная поверхность удалена.')
        paths=om.MDagPath.getAllPathsTo(self.handle.object())
        return next((p for p in paths if p.instanceNumber()==self.instance),paths[0])
    def plug(self):return self.path().fullPathName()+'.worldMesh[%d]'%self.instance
    def projector(self):return Projector(world_mesh(self.path()))


def surface_helper(node):
    return next(iter(cmds.listConnections(node+'.openingWidth',s=False,d=True,type=SURFACE_TYPE) or []),None) if SURFACE_TYPE in cmds.allNodeTypes() and cmds.objExists(node) else None


def _support_from_plug(plug):
    paths=om.MDagPath.getAllPathsTo(om.MSelectionList().add(plug.split('.')[0]).getDependNode(0))
    return Support(next((p for p in paths if plug.endswith('[%d]'%p.instanceNumber())),paths[0]))


def _validation(source,points,closed,parameters):
    if source.surface:return build_surface(points,closed,*parameters,source.surface.projector(),reverse=source.flip)
    return build(points,closed,*parameters,om.MVector(source.normal(parameters[0])).transformAsNormal(source.path().inclusiveMatrix())*(-1 if source.flip else 1))


def attach_surface(node,target,support):
    ensure_plugin(surface=True);path=om.MSelectionList().add(target).getDagPath(0)
    if path.node().hasFn(om.MFn.kTransform):path.extendToShape()
    helper=cmds.createNode(SURFACE_TYPE,name='floaterSurface#',skipSelect=True)
    try:
        cmds.connectAttr(node+'.flipDirection',helper+'.flipDirection')
        for name in ('inputCurve','curveMatrix'):
            plug=(cmds.listConnections(node+'.'+name,s=True,d=False,plugs=True) or [None])[0]
            if not plug:raise ValueError('Floater: исходная кривая удалена.')
            cmds.connectAttr(plug,helper+'.'+name)
        for name in FIELDS:cmds.connectAttr(node+'.'+name,helper+'.'+name)
        cmds.connectAttr(support.plug(),helper+'.surfaceMesh')
        if not cmds.getAttr(helper+'.valid'):raise ValueError(cmds.getAttr(helper+'.status'))
        cmds.connectAttr(helper+'.outputMesh',path.fullPathName()+'.inMesh',force=True)
        return helper
    except Exception:
        cmds.delete(helper);raise


def _from_node(node):
    sources=cmds.listConnections(node+'.inputCurve',s=True,d=False,plugs=True) or []
    if not sources:raise ValueError('Floater: у выбранного меша нет исходной кривой.')
    shape=sources[0].split('.')[0];paths=om.MDagPath.getAllPathsTo(om.MSelectionList().add(shape).getDependNode(0))
    matrix=cmds.listConnections(node+'.curveMatrix',s=True,d=False,plugs=True) or []
    path=next((p for p in paths if matrix and matrix[0].endswith('[%d]'%p.instanceNumber())),paths[0])
    source=Source(path,node);source.flip=bool(cmds.getAttr(node+'.flipDirection')) if cmds.objExists(node+'.flipDirection') else False;source.hint=tuple(cmds.getAttr(node+'.normalHint')[0])
    helper=surface_helper(node)
    if helper:
        plugs=cmds.listConnections(helper+'.surfaceMesh',s=True,d=False,plugs=True) or []
        if not plugs:raise ValueError('Floater: опорная поверхность удалена.')
        source.surface=_support_from_plug(plugs[0])
    return source


def convert_selected_edges():
    """Native, undoable edge-to-curve conversion before the Floater preview."""
    from model_pack_edge_flow import chains
    original=cmds.ls(sl=True,long=True) or [];groups={};unsupported=False
    # A Maya selection-list entry can contain several component kinds while
    # getComponent() returns only the first. Resolve the canonical strings
    # individually so an edge/face mixture cannot silently lose its faces.
    for name in original:
        try:path,component=om.MSelectionList().add(name).getComponent(0)
        except (RuntimeError,TypeError):continue
        if component.isNull():continue
        if path.node().hasFn(om.MFn.kTransform):path.extendToShape()
        if not component.hasFn(om.MFn.kMeshEdgeComponent):
            if path.node().hasFn(om.MFn.kMesh):unsupported=True
            continue
        fn=om.MFnMesh(path);indexed=om.MFnSingleIndexedComponent(component)
        key=(om.MObjectHandle(path.node()).hashCode(),path.instanceNumber())
        if key not in groups:groups[key]=[om.MDagPath(path),set()]
        groups[key][1].update(range(fn.numEdges) if indexed.isComplete else indexed.getElements())
    if not groups:return []
    if unsupported:raise ValueError('Floater: выберите рёбра без полигонов и вершин.')
    plans=[];owners=set()
    # Validate every chain before creating anything, including the last mesh
    # of a multi-object selection.
    for path,edges in groups.values():
        fn=om.MFnMesh(path)
        try:paths=chains(fn,tuple(sorted(edges)))
        except (ValueError,RuntimeError) as error:
            raise ValueError('Floater: выберите цепочки или петли рёбер без разветвлений.') from error
        edge_for_pair={frozenset(fn.getEdgeVertices(e)):e for e in edges}
        for vertices,closed in paths:
            pairs=list(zip(vertices,vertices[1:]))
            if closed:pairs.append((vertices[-1],vertices[0]))
            plans.append((path,[edge_for_pair[frozenset(pair)] for pair in pairs]))
        owners.add(path.fullPathName());parent=om.MDagPath(path);parent.pop();owners.add(parent.fullPathName())
    if not plans:return []
    if not cmds.undoInfo(q=True,state=True):raise ValueError('Floater: включите Undo для преобразования рёбер.')
    retained=[name for name in original if '.e[' not in name and name.split('.')[0] not in owners]
    created=[];cmds.undoInfo(openChunk=True,chunkName='Floater Edges to Curves')
    try:
        for path,edges in plans:
            cmds.select([path.fullPathName()+'.e[%d]'%e for e in edges],replace=True)
            result=cmds.polyToCurve(degree=1,form=2,
                constructionHistory=False,conformToSmoothMeshPreview=False,name='floaterEdgeCurve#') or []
            curves=[name for name in result if cmds.nodeType(name)=='transform' and
                    cmds.listRelatives(name,shapes=True,noIntermediate=True,type='nurbsCurve')]
            created.extend(curves)
            if len(curves)!=1:raise ValueError('Floater: не удалось преобразовать цепочку рёбер в кривую.')
            curve=curves[0]
            cmds.addAttr(curve,longName='draftTarget',attributeType='message',hidden=True)
            cmds.connectAttr(path.fullPathName()+'.message',curve+'.draftTarget')
            cmds.addAttr(curve,longName='draftTargetInstance',attributeType='long',defaultValue=path.instanceNumber(),hidden=True)
        cmds.select(retained+created,replace=True)
    except Exception:
        cmds.undoInfo(closeChunk=True)
        if created:cmds.undo()
        raise
    cmds.undoInfo(closeChunk=True)
    return created


def selected_sources():
    selected=om.MGlobal.getActiveSelectionList();result=[];seen=set();supports=[]
    for i in range(selected.length()):
        try:path=selected.getDagPath(i)
        except (RuntimeError,TypeError):continue
        names=[path.fullPathName()]
        if path.node().hasFn(om.MFn.kTransform):
            names=cmds.listRelatives(path.fullPathName(),allDescendents=True,fullPath=True,noIntermediate=True) or []
        for name in names:
            kind=cmds.nodeType(name);node=None
            if kind=='mesh':
                node=next((n for n in cmds.listHistory(name) or [] if cmds.nodeType(n)==TYPE),None)
                if node is None:
                    supports.append(Support(om.MSelectionList().add(name).getDagPath(0)));continue
                source=_from_node(node)
            elif kind=='nurbsCurve':
                node=next(iter(cmds.listConnections(name+'.local',s=False,d=True,type=TYPE) or []),None) if TYPE in cmds.allNodeTypes() else None
                source=_from_node(node) if node else Source(om.MSelectionList().add(name).getDagPath(0))
            else:continue
            key=(source.handle.hashCode(),source.instance,node)
            if key not in seen:seen.add(key);result.append(source)
    if not result:raise ValueError('Floater: выделите кривую, цепочку рёбер или созданный Floater.')
    supports=list({(p.handle.hashCode(),p.instance):p for p in supports}.values())
    if len(supports)>1:raise ValueError('Floater: выберите кривую и один опорный меш.')
    for source in result:
        if supports:source.surface=supports[0]
        elif source.surface is None:source.surface=nearest_support(source)
    return result


def nearest_support(source):
    path=source.path();parent=om.MDagPath(path);parent.pop()
    for name in (path.fullPathName(),parent.fullPathName()):
        for attr in ('draftTarget',):
            if cmds.objExists(name+'.'+attr):
                plugs=cmds.listConnections(name+'.'+attr,s=True,d=False,shapes=True) or []
                if plugs:
                    target=om.MSelectionList().add(plugs[0]).getDagPath(0)
                    if target.node().hasFn(om.MFn.kMesh):
                        paths=om.MDagPath.getAllPathsTo(target.node())
                        instance=cmds.getAttr(name+'.draftTargetInstance') if cmds.objExists(name+'.draftTargetInstance') else target.instanceNumber()
                        return Support(next((p for p in paths if p.instanceNumber()==instance),target))
    points,_=source.points(.1);span=max(max(p[k] for p in points)-min(p[k] for p in points) for k in range(3))
    probes=points[::max(1,len(points)//12)];candidates=[];limit=max(span*.1,1e-4)
    for name in cmds.ls(dag=True,allPaths=True,type='mesh',noIntermediate=True,visible=True,long=True) or []:
        target=om.MSelectionList().add(name).getDagPath(0)
        if not target.isVisible() or any(cmds.nodeType(n) in (TYPE,SURFACE_TYPE) for n in cmds.listHistory(name) or []):continue
        support=Support(target)
        try:
            surface=support.projector();distances=[(p-surface.closest(p)[0]).length() for p in probes]
            if max(distances)<=limit:candidates.append((sum(distances)/len(distances),support))
        except (ValueError,RuntimeError):continue
    candidates.sort(key=lambda item:item[0])
    if len(candidates)>1 and abs(candidates[0][0]-candidates[1][0])<max(span*1e-5,1e-6):
        raise ValueError('Floater: рядом несколько поверхностей. Выберите кривую вместе с нужным мешем.')
    return candidates[0][1] if candidates else None


def create(source,parameters,preview=False):
    ensure_plugin();path=source.path();points,closed=source.points(parameters[0]);matrix=path.inclusiveMatrix()
    hint=(0,1,0) if source.surface else source.normal(parameters[0]);normal=om.MVector(hint).transformAsNormal(matrix)
    _validation(source,points,closed,parameters)
    result=node=helper=None
    try:
        base=path.fullPathName().split('|')[-2]
        result=cmds.createNode('transform',name='floaterPreview#' if preview else base+'_floater#',skipSelect=True)
        shape=cmds.createNode('mesh',parent=result,skipSelect=True)
        node=cmds.createNode(TYPE,name='floaterProfile#',skipSelect=True)
        cmds.connectAttr(path.fullPathName()+'.local',node+'.inputCurve')
        cmds.connectAttr(path.fullPathName()+'.worldMatrix[%d]'%path.instanceNumber(),node+'.curveMatrix')
        cmds.setAttr(node+'.normalHint',*hint,type='float3');cmds.setAttr(node+'.flipDirection',source.flip);set_values(node,parameters)
        cmds.connectAttr(node+'.outputMesh',shape+'.inMesh')
        cmds.addAttr(node,longName='floaterTarget',attributeType='message');cmds.connectAttr(result+'.message',node+'.floaterTarget')
        if source.surface:helper=attach_surface(node,result,source.surface)
        cmds.sets(result,e=True,forceElement='initialShadingGroup')
        center=frame(points,normal,strict=False)[0]
        cmds.xform(result,objectSpace=True,pivots=tuple(center[i]*display_scale() for i in range(3)))
        check=helper or node
        if not cmds.getAttr(check+'.valid'):raise ValueError(cmds.getAttr(check+'.status'))
        return result,node
    except Exception:
        for name in (result,node,helper):
            if name and cmds.objExists(name):cmds.delete(name)
        raise


class Session:
    def __init__(self,sources=None):
        if sources is None:convert_selected_edges()
        self.sources=selected_sources() if sources is None else sources
        self.selection=om.MGlobal.getActiveSelectionList();self.records=[];self.closed=False
        first=self.sources[0]
        if first.existing:self.parameters=values(first.existing)
        else:
            points,closed=first.points(.1);scale=max(max(p[k] for p in points)-min(p[k] for p in points) for k in range(3))
            width=max(scale*.02,1e-5);self.parameters=(width,width*.3,width*.75)
            if not first.surface:first.normal(width)
            # Start narrow enough for a tight bend; explicit user values are
            # subsequently validated without silently changing their sizes.
            for attempt in range(12):
                points,closed=first.points(self.parameters[0])
                try:_validation(first,points,closed,self.parameters);break
                except OffsetError:
                    if attempt==11:raise
                    self.parameters=tuple(v*.5 for v in self.parameters)
        try:
            with transient():
                ensure_plugin()
                for source in self.sources:
                    if source.existing:
                        node=source.existing;targets=cmds.listConnections(node+'.floaterTarget',s=True,d=False) or []
                        if not targets:raise ValueError('Floater: не найден выходной меш.')
                        helper=surface_helper(node)
                        old_plug=(cmds.listConnections(helper+'.surfaceMesh',s=True,d=False,plugs=True) or [None])[0] if helper else None
                        record=dict(source=source,node=node,target=targets[0],before=values(node),new=False,
                                    surface_action=None,surface_before=old_plug,helper=helper,before_flip=bool(cmds.getAttr(node+'.flipDirection')))
                        self.records.append(record)
                        if source.surface:
                            if helper:
                                if not cmds.isConnected(source.surface.plug(),helper+'.surfaceMesh'):
                                    record['surface_action']='retarget';cmds.connectAttr(source.surface.plug(),helper+'.surfaceMesh',force=True)
                            else:
                                record['surface_action']='add';record['helper']=attach_surface(node,targets[0],source.surface)
                    else:
                        target,node=create(source,self.parameters,preview=True)
                        self.records.append(dict(source=source,node=node,target=target,before=None,new=True))
                om.MGlobal.setActiveSelectionList(self.selection)
        except Exception:self.cancel();raise
    def valid(self):
        errors=[]
        for record in self.records:
            try:record['source'].path()
            except ValueError as error:errors.append(str(error));continue
            try:
                if record['source'].surface:record['source'].surface.path()
            except ValueError as error:errors.append(str(error));continue
            node=surface_helper(record['node']) or record['node']
            if not cmds.objExists(node):errors.append('Floater: результат удалён.');continue
            if not cmds.getAttr(node+'.valid'):errors.append(cmds.getAttr(node+'.status') or 'Floater: некорректный контур.')
        return not errors,errors[0] if errors else ''
    def update(self,width,height,skirt):
        if self.closed:raise ValueError('Floater уже завершён.')
        if width<=0 or min(height,skirt)<0 or any(not math.isfinite(v) for v in (width,height,skirt)):raise ValueError('Floater: неверные размеры.')
        with transient():
            for record in self.records:set_values(record['node'],(width,height,skirt))
        self.parameters=width,height,skirt
        return self.valid()
    def set_direction(self,flip):
        if self.closed:raise ValueError('Floater уже завершён.')
        with transient():
            for record in self.records:
                record['source'].flip=bool(flip)
                cmds.setAttr(record['node']+'.flipDirection',bool(flip))
        return self.valid()
    def cleanup(self):
        with transient():
            for record in self.records:
                if record['new']:
                    for name in (record['target'],surface_helper(record['node']) if cmds.objExists(record['node']) else None,record['node']):
                        if name and cmds.objExists(name):cmds.delete(name)
                elif cmds.objExists(record['node']):
                    if record.get('surface_action')=='add':
                        if cmds.objExists(record['target']):
                            shape=cmds.listRelatives(record['target'],shapes=True,fullPath=True)[0]
                            cmds.connectAttr(record['node']+'.outputMesh',shape+'.inMesh',force=True)
                        if record['helper'] and cmds.objExists(record['helper']):cmds.delete(record['helper'])
                    elif record.get('surface_action')=='retarget' and cmds.objExists(record['helper']) and record['surface_before'] and cmds.objExists(record['surface_before']):
                        cmds.connectAttr(record['surface_before'],record['helper']+'.surfaceMesh',force=True)
                    set_values(record['node'],record['before'])
                    cmds.setAttr(record['node']+'.flipDirection',record['before_flip'])
    def cancel(self):
        if self.closed:return
        self.cleanup();self.closed=True
        try:
            with transient():om.MGlobal.setActiveSelectionList(self.selection)
        except RuntimeError:pass
    def commit(self):
        if self.closed:return []
        valid,error=self.valid()
        if not valid:raise ValueError(error)
        if not cmds.undoInfo(q=True,state=True):raise ValueError('Floater: включите Undo для применения.')
        pending=[values(record['node']) for record in self.records]
        for source in self.sources:source.path()
        if all(not r['new'] and not r.get('surface_action') and r['source'].flip==r['before_flip'] and all(abs(a-b)<1e-12 for a,b in zip(parameters,r['before'])) for r,parameters in zip(self.records,pending)):
            self.closed=True;return [r['target'] for r in self.records]
        self.cleanup();changed=False;results=[];cmds.undoInfo(openChunk=True,chunkName='Floater')
        try:
            for record,parameters in zip(self.records,pending):
                if record['new']:
                    target,node=create(record['source'],parameters);changed=True
                else:
                    target=record['target'];changed=True;set_values(record['node'],parameters)
                    cmds.setAttr(record['node']+'.flipDirection',record['source'].flip)
                    if record.get('surface_action')=='add':attach_surface(record['node'],target,record['source'].surface)
                    elif record.get('surface_action')=='retarget':cmds.connectAttr(record['source'].surface.plug(),record['helper']+'.surfaceMesh',force=True)
                results.append(target)
            cmds.select(results,replace=True)
        except Exception:
            cmds.undoInfo(closeChunk=True)
            if changed:cmds.undo()
            self.closed=True;raise
        cmds.undoInfo(closeChunk=True);self.closed=True;return results


def active():return _HOST is not None and not _HOST.stopped


def shutdown(apply=False):
    global _HOST
    host,_HOST=_HOST,None
    if host is not None and isValid(host):host.stop(apply)


def toggle(window):
    global _HOST
    if active() and _HOST.window() is window:shutdown(apply=True);return
    shutdown();window._prepare_tool('floater');_HOST=Host(window)
    try:_HOST.start()
    except Exception as error:shutdown();cmds.warning(str(error))


def set_button(window,checked):
    window.floater_button.setChecked(checked)
    window.floater_button.setIcon(QtGui.QIcon(str(Path(__file__).resolve().parent/'icons'/('floater_active.svg' if checked else 'floater.svg'))))


def sync(window):
    checked=active() and _HOST.window() is window
    if window.floater_button.isChecked()!=checked:set_button(window,checked)


class Host(QtCore.QObject):
    def __init__(self,window):
        super().__init__(window);self.window_ref=weakref.ref(window);self.session=None;self.panel=None;self.stopped=False;self.callbacks=[]
        self.compact_size=QtCore.QSize(window.size());self.drag=None
        self.history=None;self.undo_group=None
    def window(self):
        value=self.window_ref();return value if value is not None and isValid(value) else None
    def start(self):
        from model_pack_theme import UndoGroup
        self.undo_group=UndoGroup('Floater')
        self.session=Session();window=self.window();self.panel=QtWidgets.QWidget(window);self.panel.setObjectName('modelPackFloaterSettings')
        layout=QtWidgets.QVBoxLayout(self.panel);layout.setContentsMargins(0,5,0,0);layout.setSpacing(5);self.fields={}
        for label,name,value in zip(('Opening Width','Height','Skirt'),FIELDS,self.session.parameters):
            row=QtWidgets.QHBoxLayout();row.addWidget(QtWidgets.QLabel(label,self.panel))
            field=QtWidgets.QDoubleSpinBox(self.panel);field.setObjectName('floater'+name);field.setDecimals(3)
            field.setRange(.001 if name=='openingWidth' else 0.,1000000.);field.setKeyboardTracking(False)
            field.setSingleStep(max(value*display_scale()*.05,.001));field.setValue(value*display_scale());row.addWidget(field);layout.addLayout(row)
            field.valueChanged.connect(self.change);self.fields[name]=field
        self.flip=QtWidgets.QCheckBox('Flip Direction',self.panel)
        self.flip.setChecked(self.session.sources[0].flip)
        self.flip.toggled.connect(self.change_direction);layout.addWidget(self.flip)
        self.hint=QtWidgets.QLabel('Shift + LMB — Width\nShift + Ctrl + LMB — Height\nShift + Alt + LMB — Skirt\nEnter — Apply · Esc — Cancel',self.panel)
        self.hint.setWordWrap(True);self.hint.setAlignment(QtCore.Qt.AlignCenter);self.hint.setStyleSheet('color:#a4adb7;font-size:10px;');layout.addWidget(self.hint)
        self.surface_label=QtWidgets.QLabel(self.panel);self.surface_label.setWordWrap(True)
        supports=[s.surface.path().partialPathName() for s in self.session.sources if s.surface]
        self.surface_label.setText('Surface: '+', '.join(dict.fromkeys(supports)) if supports else 'Free Curve · Select curve + mesh to fit a surface')
        layout.addWidget(self.surface_label)
        self.status=QtWidgets.QLabel(self.panel);self.status.setWordWrap(True);self.status.setStyleSheet('color:#ef9a63;');layout.addWidget(self.status)
        row=QtWidgets.QHBoxLayout();self.apply_button=QtWidgets.QPushButton('Apply',self.panel);self.cancel_button=QtWidgets.QPushButton('Cancel',self.panel)
        for button in (self.apply_button,self.cancel_button):button.setMinimumHeight(30);button.setAutoDefault(False);row.addWidget(button)
        self.apply_button.setProperty('primary',True);self.apply_button.clicked.connect(lambda:shutdown(apply=True));self.cancel_button.clicked.connect(lambda:shutdown())
        layout.addLayout(row);window.layout().addWidget(self.panel,window._panel_row,0,1,2);self.panel.show();set_button(window,True)
        self.timer=QtCore.QTimer(self);self.timer.setInterval(180);self.timer.timeout.connect(self.poll);self.timer.start()
        self.callbacks=[om.MSceneMessage.addCallback(kind,lambda *_:shutdown()) for kind in (om.MSceneMessage.kBeforeNew,om.MSceneMessage.kBeforeOpen)]
        QtWidgets.QApplication.instance().installEventFilter(self);self.poll();window.layout().activate();window.resize(window.width(),window.sizeHint().height())
        if any(abs(self.fields[name].value()-value*display_scale())>1e-9 for name,value in zip(FIELDS,self.session.parameters)):self.change()
        from model_pack_theme import PreviewHistory
        def restore():
            self.session.set_direction(self.flip.isChecked());self.change()
        self.history=PreviewHistory(self,tuple(self.fields[name] for name in FIELDS)+(self.flip,),restore)
    def change(self,*_):
        if self.stopped:return
        try:self.session.update(*(self.fields[name].value()/display_scale() for name in FIELDS));self.poll();cmds.refresh(currentView=True)
        except (ValueError,RuntimeError) as error:self.status.setText(str(error));self.apply_button.setEnabled(False)
    def change_direction(self,value):
        if self.stopped:return
        try:self.session.set_direction(value);self.poll();cmds.refresh(currentView=True)
        except (ValueError,RuntimeError) as error:self.status.setText(str(error));self.apply_button.setEnabled(False)
    def poll(self):
        if self.stopped:return
        valid,error=self.session.valid();self.apply_button.setEnabled(valid);self.status.setText(error);self.status.setVisible(bool(error))
    def begin_drag(self,point,name):
        if self.history:self.history.begin()
        scale=max(field.value() for field in self.fields.values())
        self.drag=dict(anchor=point,name=name,start=self.fields[name].value(),scale=max(scale,.001),axis=None)
    def move_drag(self,point):
        drag=self.drag;delta=(point.x()-drag['anchor'].x(),drag['anchor'].y()-point.y())
        if drag['axis'] is None:
            if max(map(abs,delta))<3:return
            drag['axis']=0 if abs(delta[0])>=abs(delta[1]) else 1
        self.fields[drag['name']].setValue(drag['start']+delta[drag['axis']]*drag['scale']/200.)
    def finish_drag(self,cancel=False):
        drag,self.drag=self.drag,None
        if drag is None:return
        if cancel:self.fields[drag['name']].setValue(drag['start'])
        if self.history:self.history.end()
    def eventFilter(self,target,event):
        if self.stopped:return False
        kind=event.type()
        if self.drag is not None:
            if kind==QtCore.QEvent.MouseMove:
                if event.buttons()&QtCore.Qt.LeftButton:self.move_drag(event.globalPosition().toPoint())
                else:self.finish_drag()
                return True
            if kind==QtCore.QEvent.MouseButtonRelease and event.button()==QtCore.Qt.LeftButton:
                self.move_drag(event.globalPosition().toPoint())
                self.finish_drag()
                return True
            if kind==QtCore.QEvent.MouseButtonPress and event.button()!=QtCore.Qt.LeftButton:self.finish_drag()
            if kind==QtCore.QEvent.KeyPress and event.key()==QtCore.Qt.Key_Escape:self.finish_drag(cancel=True);return True
            if kind==QtCore.QEvent.KeyRelease and event.key() in (QtCore.Qt.Key_Shift,QtCore.Qt.Key_Control,QtCore.Qt.Key_Alt):self.finish_drag();return True
            if kind in (QtCore.QEvent.ApplicationDeactivate,QtCore.QEvent.WindowDeactivate):self.finish_drag()
        if isinstance(target,(QtWidgets.QLineEdit,QtWidgets.QAbstractSpinBox,QtWidgets.QTextEdit,QtWidgets.QPlainTextEdit)):return False
        if kind in (QtCore.QEvent.MouseButtonPress,QtCore.QEvent.MouseButtonDblClick) and event.button()==QtCore.Qt.LeftButton:
            name={QtCore.Qt.ShiftModifier:'openingWidth',
                  QtCore.Qt.ShiftModifier|QtCore.Qt.ControlModifier:'height',
                  QtCore.Qt.ShiftModifier|QtCore.Qt.AltModifier:'skirtLength'}.get(event.modifiers())
            if name:
                from model_pack_tube_viewport import viewport_widget
                point=event.globalPosition().toPoint()
                if viewport_widget(target,point) is not None:self.begin_drag(point,name);return True
        if kind not in (QtCore.QEvent.KeyPress,QtCore.QEvent.ShortcutOverride) or event.modifiers()!=QtCore.Qt.NoModifier:return False
        window=self.window();belongs=window is not None and (target is window or window.isAncestorOf(target))
        if not belongs:
            from model_pack_tube_viewport import viewport_widget
            belongs=viewport_widget(target,QtGui.QCursor.pos()) is not None
        if not belongs:return False
        if event.key() not in (QtCore.Qt.Key_Return,QtCore.Qt.Key_Enter,QtCore.Qt.Key_Escape):return False
        if event.type()==QtCore.QEvent.KeyPress and not event.isAutoRepeat():
            if event.key()==QtCore.Qt.Key_Escape:shutdown()
            elif self.apply_button.isEnabled():shutdown(apply=True)
        event.accept();return True
    def stop(self,apply=False):
        if self.stopped:return
        self.finish_drag()
        if self.history:self.history.close()
        self.stopped=True
        if hasattr(self,'timer'):self.timer.stop()
        app=QtWidgets.QApplication.instance()
        if app:app.removeEventFilter(self)
        for callback in self.callbacks:om.MMessage.removeCallback(callback)
        self.callbacks=[]
        try:
            if self.session:
                if apply:
                    try:self.session.commit()
                    except Exception as error:self.session.cancel();cmds.warning(str(error))
                else:self.session.cancel()
        finally:
            if self.undo_group:self.undo_group.close();self.undo_group=None
            window=self.window()
            if self.panel is not None and isValid(self.panel):
                self.panel.hide()
                if window is not None:window.layout().removeWidget(self.panel)
                self.panel.deleteLater()
            if window is not None:
                if window._active_tool=='floater':window._active_tool=None
                set_button(window,False);window.layout().activate();window.resize(self.compact_size)
            self.deleteLater()
