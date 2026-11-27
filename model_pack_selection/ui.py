"""Maya entry points and a click-to-fill polygon region context."""
from maya import cmds,mel
from maya.api import OpenMaya as om,OpenMayaUI as omui
from model_pack_bevel.session import without_undo
from slide_for_maya.core import _mesh_fn
from . import core,path_mode,repeat

CONTEXT="modelPackSelectionRegionContext"
_BOUNDARIES={}
_IDENTITIES={}
_ANCHOR=None
_BUTTON=1
_MODIFIERS=None
_MOVED=False
_GROW_MODE=None
_GROW_IDENTITIES={}
_GROW_STATES={}


def _reset_growth():
    global _GROW_MODE,_GROW_IDENTITIES,_GROW_STATES
    _GROW_MODE=None;_GROW_IDENTITIES={};_GROW_STATES={}


def _growth_key(selection):
    return tuple((mesh,frozenset(faces)) for mesh,faces in sorted(selection.items()))


def targets(*,faces_only=False):
    result={}
    items=om.MGlobal.getActiveSelectionList()
    for i in range(items.length()):
        try:path,component=items.getComponent(i)
        except RuntimeError:continue
        if path.node().hasFn(om.MFn.kTransform):
            if faces_only:raise ValueError("Выберите полигоны для этой операции.")
            names=cmds.listRelatives(path.fullPathName(),shapes=True,noIntermediate=True,type="mesh",fullPath=True) or []
        elif path.node().hasFn(om.MFn.kMesh):names=[path.fullPathName()]
        else:continue
        if not component.isNull() and not component.hasFn(om.MFn.kMeshPolygonComponent):
            raise ValueError("Инструмент работает с полигонами. Выберите их в режиме F11.")
        if component.isNull() and faces_only:raise ValueError("Выберите полигоны для этой операции.")
        faces=set(om.MFnSingleIndexedComponent(component).getElements()) if not component.isNull() else set()
        for name in names:result.setdefault(name,set()).update(faces)
    if not result:raise ValueError("Выберите меш или его полигоны.")
    return result


def _apply(results,title,*,kind='face'):
    """All targets are preflighted before one native, undoable selection."""
    cmds.undoInfo(openChunk=True,chunkName=title)
    try:
        cmds.selectMode(component=True)
        masks={'vertex':'polymeshVertex','edge':'polymeshEdge','face':'polymeshFace'}
        cmds.selectType(allComponents=False);cmds.selectType(**{masks[kind]:True})
        parents=[]
        for mesh in results:
            path=_mesh_fn(mesh).dagPath();path.pop();parents.append(path.fullPathName())
        cmds.hilite(parents,replace=True)
        suffix={'vertex':'vtx','edge':'e','face':'f'}[kind]
        faces=[mesh+f".{suffix}[{f}]" for mesh,ids in results.items() for f in sorted(ids)]
        if faces:cmds.select(faces,replace=True)
        else:cmds.select(clear=True)
    finally:cmds.undoInfo(closeChunk=True)


def path_targets():
    message='Path: выберите ровно две вершины, два ребра или два полигона одного меша.'
    items=om.MGlobal.getActiveSelectionList();target=None;kind=None;ids=set()
    kinds={om.MFn.kMeshVertComponent:'vertex',om.MFn.kMeshEdgeComponent:'edge',om.MFn.kMeshPolygonComponent:'face'}
    for index in range(items.length()):
        try:dag,component=items.getComponent(index)
        except RuntimeError:raise ValueError(message) from None
        if component.isNull() or not dag.node().hasFn(om.MFn.kMesh) or component.apiType() not in kinds:
            raise ValueError(message)
        name=dag.fullPathName();current=kinds[component.apiType()]
        if target is not None and (name!=target or kind!=current):raise ValueError(message)
        target=name;kind=current;ids.update(om.MFnSingleIndexedComponent(component).getElements())
    if len(ids)!=2:raise ValueError(message)
    return target,kind,sorted(ids)


def path():
    try:
        mesh,kind,(start,end)=path_targets()
        selected=core.shortest_path(_mesh_fn(mesh),kind,start,end)
        # Failed/ambiguous requests leave selection, context and Undo intact.
        shutdown()
        _apply({mesh:selected},'Selection Path',kind=kind)
        return True
    except (ValueError,RuntimeError) as error:
        cmds.warning('Выделение: '+str(error));return False


def toggle_path(window):
    if path_mode.enabled():path_mode.shutdown();return False
    shutdown()
    if cmds.currentCtx() not in path_mode.NATIVE_CONTEXTS:cmds.setToolTo('selectSuperContext')
    return path_mode.enable(window)


def path_enabled():return path_mode.enabled()


def grow(*,shrink=False,modifiers=None):
    global _GROW_MODE,_GROW_IDENTITIES,_GROW_STATES
    shutdown(clear_growth=False)
    try:
        modifiers=cmds.getModifiers() if modifiers is None else modifiers
        if modifiers&4 and modifiers&1:raise ValueError("Ctrl — широкое направление, Shift — узкое. Зажмите один из них.")
        kind,components=periodic_targets() if cmds.filterExpand(selectionMask=(31,32)) else (None,{})
        if kind in ('vertex','edge'):
            _reset_growth()
            results={mesh:repeat.grow_components(core.Mesh(mesh),ids,kind,shrink=shrink,
                     narrow=bool(modifiers&1) if modifiers&5 else None) for mesh,ids in components.items()}
            _apply(results,'Selection shrink' if shrink else 'Selection grow',kind=kind)
            return True
        if not modifiers&5:
            _reset_growth()
            # Use the same traversal as Maya's PolySelectTraverse, restoring
            # the user's persistent constraint settings afterwards.
            if not cmds.filterExpand(selectionMask=(31,32,34,35)):
                raise ValueError("Выберите компоненты меша.")
            original=om.MGlobal.getActiveSelectionList()
            state=cmds.polySelectConstraint(query=True,stateString=True)
            masks={31:1,32:0x8000,34:8,35:0x10}
            mask=next(kind for kind in masks if cmds.filterExpand(selectionMask=kind))
            with without_undo():
                try:
                    cmds.polySelectConstraint(disable=True)
                    cmds.polySelectConstraint(propagate=2 if shrink else 1,type=masks[mask])
                    selected=cmds.ls(selection=True,flatten=True,long=True) or []
                finally:
                    mel.eval(state);om.MGlobal.setActiveSelectionList(original)
            if selected:cmds.select(selected,replace=True)
            else:cmds.select(clear=True)
        else:
            selection=targets(faces_only=True)
            data={mesh:core.Mesh(mesh) for mesh in selection}
            identities={mesh:mesh_data.identity for mesh,mesh_data in data.items()}
            narrow=bool(modifiers&1);key=_growth_key(selection)
            # Keep recent selections so Undo/Redo can resume the same axes.
            states=_GROW_STATES.copy() if narrow==_GROW_MODE and identities==_GROW_IDENTITIES else {}
            if key not in states:states={}
            directions={mesh:dict(states.get(key,{}).get(mesh,{})) for mesh in selection}
            results={mesh:core.grow(data[mesh],faces,narrow=narrow,shrink=shrink,directions=directions[mesh])
                     for mesh,faces in selection.items()}
            _apply(results,"Selection shrink" if shrink else "Selection grow")
            states.setdefault(key,{mesh:{f:axis for f,axis in directions[mesh].items() if f in selection[mesh]}
                                   for mesh in selection})
            states[_growth_key(results)]={mesh:{f:directions[mesh][f] for f in faces} for mesh,faces in results.items()}
            while len(states)>32:states.pop(next(iter(states)))
            _GROW_MODE=narrow;_GROW_IDENTITIES=identities;_GROW_STATES=states
        return True
    except (ValueError,RuntimeError) as error:
        cmds.warning("Выделение: "+str(error));return False


def periodic_targets():
    result={};kind=None
    kinds={om.MFn.kMeshVertComponent:'vertex',om.MFn.kMeshEdgeComponent:'edge',om.MFn.kMeshPolygonComponent:'face'}
    # A single Maya selection entry can contain both face and edge components
    # on one DAG object; getComponent alone exposes only one of those types.
    # Resolve its serialized component ranges separately before choosing a kind.
    names=om.MGlobal.getActiveSelectionList().getSelectionStrings()
    for name in names:
        try:
            item=om.MSelectionList();item.add(name);path,component=item.getComponent(0)
        except RuntimeError:raise ValueError('Выберите группу полигонов, рёбер или вершин.') from None
        if component.isNull() or component.apiType() not in kinds:raise ValueError('Выберите группу полигонов, рёбер или вершин.')
        current=kinds[component.apiType()]
        if kind is not None and current!=kind:raise ValueError('Выберите один тип компонентов: полигоны, рёбра или вершины.')
        kind=current
        if path.node().hasFn(om.MFn.kTransform):path.extendToShape()
        if not path.node().hasFn(om.MFn.kMesh):raise ValueError('Для повторения нужны компоненты полигонального меша.')
        fn=om.MFnMesh(path);indices=om.MFnSingleIndexedComponent(component)
        ids=range({'vertex':fn.numVertices,'edge':fn.numEdges,'face':fn.numPolygons}[kind]) if indices.isComplete else indices.getElements()
        result.setdefault(path.fullPathName(),set()).update(ids)
    if not result or not any(result.values()):raise ValueError('Выберите группу полигонов, рёбер или вершин.')
    return kind,result


def periodic(*,across=False,skip=1):
    try:
        kind,selection=periodic_targets();operation={'edge':repeat.edges,'face':repeat.faces,'vertex':repeat.vertices}[kind]
        results={mesh:operation(core.Mesh(mesh),ids,across=across,skip=skip) for mesh,ids in selection.items()}
        shutdown()
        _apply(results,"Selection repeat groups",kind=kind)
        return True
    except (ValueError,RuntimeError) as error:
        cmds.warning("Выделение: "+str(error));return False


def active():return cmds.currentCtx()==CONTEXT


def activate_region():
    global _BOUNDARIES,_IDENTITIES
    path_mode.shutdown()
    _reset_growth()
    if active():shutdown();return False
    cmds.setToolTo("selectSuperContext")
    try:
        _BOUNDARIES=targets()
        _IDENTITIES={mesh:core.Mesh(mesh).identity for mesh in _BOUNDARIES}
        original=om.MGlobal.getActiveSelectionList()
        with without_undo():
            cmds.selectMode(component=True);cmds.selectType(allComponents=False);cmds.selectType(polymeshFace=True)
            cmds.hilite(list(_BOUNDARIES),replace=True)
            om.MGlobal.setActiveSelectionList(original)
        callbacks=dict(pressCommand=_press,dragCommand=_drag,releaseCommand=_release,finalize=_finalize,
                       space="screen",undoMode="none",cursor="crossHair")
        if cmds.draggerContext(CONTEXT,exists=True):cmds.draggerContext(CONTEXT,edit=True,**callbacks)
        else:cmds.draggerContext(CONTEXT,**callbacks)
        cmds.setToolTo(CONTEXT)
        cmds.inViewMessage(amg="Выберите полигон внутри контура · Shift — сохранить контур",pos="midCenterTop",fade=True)
        return True
    except (ValueError,RuntimeError) as error:
        _finalize();cmds.warning("Выделение: "+str(error));return False


def _pick(point):
    view=omui.M3dView.active3dView();origin,direction=om.MPoint(),om.MVector()
    view.viewToWorld(int(point[0]),int(point[1]),origin,direction)
    hits=[]
    for mesh in _BOUNDARIES:
        if not cmds.objExists(mesh):continue
        fn=_mesh_fn(mesh)
        hit=fn.closestIntersection(om.MFloatPoint(origin),om.MFloatVector(direction),
                                   om.MSpace.kWorld,1e10,False,accelParams=fn.autoUniformGridParams())
        if hit is not None:hits.append((hit[1],mesh,hit[2]))
    if not hits:raise ValueError("Кликните по поверхности выбранного меша.")
    _,mesh,face=min(hits)
    return mesh,face


def _press():
    global _ANCHOR,_BUTTON,_MODIFIERS,_MOVED
    _ANCHOR=cmds.draggerContext(CONTEXT,query=True,anchorPoint=True)
    _BUTTON=cmds.draggerContext(CONTEXT,query=True,button=True)
    _MODIFIERS=cmds.getModifiers();_MOVED=False


def _drag():
    global _MOVED
    if _ANCHOR is not None:
        point=cmds.draggerContext(CONTEXT,query=True,dragPoint=True)
        if sum((a-b)**2 for a,b in zip(_ANCHOR[:2],point[:2]))>36:_MOVED=True


def _release():
    global _ANCHOR,_MODIFIERS
    anchor,_ANCHOR=_ANCHOR,None
    modifiers,_MODIFIERS=_MODIFIERS,None
    if anchor is None:return
    # A plain click may have no dragPoint update. Track actual drag events
    # and the pressed button rather than querying their release-time values.
    if _MOVED or _BUTTON!=1:return
    try:
        mesh,seed=_pick(anchor);data=core.Mesh(mesh)
        if data.identity!=_IDENTITIES[mesh]:
            raise ValueError("Сетка изменилась. Снова включите выделение области.")
        keep=bool((cmds.getModifiers() if modifiers is None else modifiers)&1)
        selected=core.region(data,_BOUNDARIES[mesh],seed,keep_boundary=keep)
        results=dict(_BOUNDARIES) if keep else {}
        results[mesh]=selected
        _apply(results,"Selection region")
        shutdown()
    except (ValueError,RuntimeError) as error:cmds.warning("Выделение: "+str(error))


def _finalize():
    global _BOUNDARIES,_IDENTITIES,_ANCHOR,_BUTTON,_MODIFIERS,_MOVED
    _BOUNDARIES={};_IDENTITIES={};_ANCHOR=None
    _BUTTON=1;_MODIFIERS=None;_MOVED=False


def shutdown(*,clear_growth=True):
    path_mode.shutdown()
    if active():cmds.setToolTo("selectSuperContext")
    _finalize()
    if clear_growth:_reset_growth()

