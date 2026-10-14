"""Maya entry points and a click-to-fill polygon region context."""
from maya import cmds,mel
from maya.api import OpenMaya as om,OpenMayaUI as omui
from model_pack_bevel.session import without_undo
from slide_for_maya.core import _mesh_fn
from . import core

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


def _apply(results,title):
    """All targets are preflighted before one native, undoable selection."""
    cmds.undoInfo(openChunk=True,chunkName=title)
    try:
        cmds.selectMode(component=True)
        cmds.selectType(allComponents=False);cmds.selectType(polymeshFace=True)
        parents=[]
        for mesh in results:
            path=_mesh_fn(mesh).dagPath();path.pop();parents.append(path.fullPathName())
        cmds.hilite(parents,replace=True)
        faces=[mesh+f".f[{f}]" for mesh,ids in results.items() for f in sorted(ids)]
        if faces:cmds.select(faces,replace=True)
        else:cmds.select(clear=True)
    finally:cmds.undoInfo(closeChunk=True)


def grow(*,shrink=False,modifiers=None):
    global _GROW_MODE,_GROW_IDENTITIES,_GROW_STATES
    shutdown(clear_growth=False)
    try:
        modifiers=cmds.getModifiers() if modifiers is None else modifiers
        if modifiers&4 and modifiers&1:raise ValueError("Ctrl — широкое направление, Shift — узкое. Зажмите один из них.")
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


def periodic(*,across=False,skip=1):
    shutdown()
    try:
        results={mesh:core.periodic(core.Mesh(mesh),faces,across=across,skip=skip)
                 for mesh,faces in targets(faces_only=True).items()}
        _apply(results,"Selection every N polygons")
        return True
    except (ValueError,RuntimeError) as error:
        cmds.warning("Выделение: "+str(error));return False


def active():return cmds.currentCtx()==CONTEXT


def activate_region():
    global _BOUNDARIES,_IDENTITIES
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
    if active():cmds.setToolTo("selectSuperContext")
    _finalize()
    if clear_growth:_reset_growth()
