"""Model Pack entry point for Joe Wu's supplied unBevel 1.54 script.

The original file stays intact. Load its definitions without the standalone
startup call, then isolate its temporary names and attach lifecycle callbacks.
"""
import ast
import itertools
import math
from pathlib import Path
import types

import maya.cmds as cmds
import maya.mel as mel
from maya.api import OpenMaya as om
from .session import edge_selection,without_undo
from slide_for_maya.core import _mesh_fn

SCRIPT_PATH = Path(__file__).with_name("unBevel1.54.py")
CONTEXT = "modelPackUnBevelContext"
_BACKEND = None
_STAMP = None
_RUNNING = False
_PRESSED = False
_FINISHING = False
_MESH = None
_NAMES = {}
_SELECTION = None
_INPUT_MODE = "edge"
_FACE_DETAILS = {}
_FACE_MAPS = None


def _component_selection(mesh):
    if _INPUT_MODE == "face":
        cmds.selectMode(component=True)
        cmds.selectType(allComponents=False)
        cmds.selectType(polymeshFace=True)
        cmds.hilite(mesh, replace=True)
    else:
        edge_selection(mesh)


def _mapped(value):
    if isinstance(value,str):
        return _NAMES.get(value,value)
    if isinstance(value,(list,tuple)):
        return type(value)(_mapped(item) for item in value)
    return value


class _Commands:
    """Delegate Maya commands while isolating the vendor's helper names."""
    def __getattr__(self,name):
        command = getattr(cmds,name)
        def call(*args,**kwargs):
            global _SELECTION
            args = tuple(_mapped(value) for value in args)
            kwargs = {key:_mapped(value) for key,value in kwargs.items()}
            helper = _NAMES.get("saveSel")
            # Native component sets insert groupParts history. Deleting that
            # temporary history around polyMergeVertex(ch=0) makes Maya 2027
            # undo hold stale node references. Keep this set in memory instead.
            if name=="sets" and kwargs.get("name")==helper:
                _SELECTION = cmds.ls(sl=True,flatten=True,long=True) or []
                return helper
            if args and args[0]==helper and name in ("objExists","delete","select"):
                if name=="objExists":return _SELECTION is not None
                if name=="delete":
                    _SELECTION = None
                    return None
                selected = om.MSelectionList()
                for component in _SELECTION or []:selected.add(component)
                om.MGlobal.setActiveSelectionList(selected)
                return None
            if name=="polyMergeVertex":
                return _merge(command,args,kwargs)
            if name=="draggerContext" and not any(kwargs.get(key) for key in ("q","query","exists","ex")):
                kwargs.pop("rc",None)
                kwargs.update(pressCommand=_press,dragCommand=_drag,releaseCommand=_release,
                              finalize=_finalize,space="screen",undoMode="step")
            if name=="headsUpDisplay" and (kwargs.get("rem") or kwargs.get("remove")):
                if not cmds.headsUpDisplay(args[0],exists=True):
                    return None
            if name=="headsUpDisplay" and "section" in kwargs:
                if cmds.headsUpDisplay(args[0],exists=True):
                    cmds.headsUpDisplay(args[0],remove=True)
                kwargs["block"] = cmds.headsUpDisplay(nextFreeBlock=kwargs["section"])
            return command(*args,**kwargs)
        return call


class _Mel:
    def eval(self,command):
        # This GUI MEL procedure is missing in standalone and some Maya setups.
        if command.startswith('doMenuNURBComponentSelection('):
            _component_selection(_MESH)
            return None
        return mel.eval(command)


def _merge(command,args,kwargs):
    """Track surviving selected components through merge and local cleanup."""
    global _SELECTION
    fn = _mesh_fn(_MESH)
    before = fn.getPoints(om.MSpace.kWorld)
    tracked = [fn.getPolygonVertices(i) for i in range(fn.numPolygons)] if _FACE_MAPS is not None else None
    pairs=[]
    for component in _SELECTION or []:
        index=int(component.rsplit("[",1)[1][:-1])
        pairs.append(fn.getPolygonVertices(index) if _INPUT_MODE == "face" else fn.getEdgeVertices(index))
    roots = [before[fan["root"]] for fan in _FACE_DETAILS.get("fans",())
             if max((before[v]-before[fan["arc"][0]]).length() for v in fan["arc"])<1e-7]
    answer = command(*args,**kwargs)
    if roots:
        _dissolve_fan_roots(roots)
    _remap_selection(before,pairs,float(kwargs.get("d",.001)))
    if _INPUT_MODE=="face" and _FACE_DETAILS.get("regions") and _repair_collapsed_faces():
        _remap_selection(before,pairs,float(kwargs.get("d",.001)))
    if tracked is not None:
        mapping=_component_remap(before,tracked,float(kwargs.get("d",.001)),"face")
        mesh=_mesh_fn(_MESH).fullPathName()
        previous=_FACE_MAPS.setdefault(mesh,{i:i for i in range(len(tracked))})
        _FACE_MAPS[mesh]={old:mapping[current] for old,current in previous.items() if current in mapping}
    return answer


def _remap_selection(before,pairs,merge_distance):
    global _SELECTION
    mapping=_component_remap(before,pairs,merge_distance,_INPUT_MODE)
    token="f" if _INPUT_MODE=="face" else "e"
    _SELECTION=[_MESH+f".{token}[{index}]" for index in sorted(set(mapping.values()))]


def _component_remap(before,pairs,merge_distance,mode):
    """Match complete surviving polygons/edges, excluding collapsed cells."""
    fn = _mesh_fn(_MESH)
    after = fn.getPoints(om.MSpace.kWorld)
    matrix=fn.dagPath().inclusiveMatrix()
    scale=max(om.MVector(tuple(matrix[i*4+j] for j in range(3))).length() for i in range(3))
    tolerance=max(merge_distance*scale*1.01,1e-8)
    def key(point):return tuple(math.floor(point[i]/tolerance) for i in range(3))
    buckets={}
    for index,point in enumerate(after):buckets.setdefault(key(point),[]).append(index)
    remap={}
    for vertex in {v for pair in pairs for v in pair}:
        center=key(before[vertex])
        candidates=[index for delta in itertools.product((-1,0,1),repeat=3)
                    for index in buckets.get(tuple(a+b for a,b in zip(center,delta)),())]
        closest=min(candidates,key=lambda index:(after[index]-before[vertex]).length(),default=None)
        if closest is not None and (after[closest]-before[vertex]).length()<=tolerance:
            remap[vertex]=closest
    minimum = 3 if mode == "face" else 2
    output={frozenset(fn.getPolygonVertices(i) if mode=="face" else fn.getEdgeVertices(i)):i
            for i in range(fn.numPolygons if mode=="face" else fn.numEdges)}
    result={}
    for index,pair in enumerate(pairs):
        if not all(v in remap for v in pair):continue
        vertices=frozenset(remap[v] for v in pair)
        if len(vertices)>=minimum and vertices in output:result[index]=output[vertices]
    return result


def _repair_collapsed_faces():
    """Delete collapsed selected polygons and stitch partial cuts locally."""
    fn=_mesh_fn(_MESH);points=fn.getPoints(om.MSpace.kWorld)
    polygon=om.MItMeshPolygon(fn.dagPath());bad=[];splits=[]
    for component in _SELECTION or []:
        index=int(component.rsplit("[",1)[1][:-1])
        polygon.setIndex(index)
        vertices=list(polygon.getVertices())
        # Maya stores moved points with finite precision. A collapsed face
        # on a rotated mesh can retain a tiny measured area; test its distance
        # from the longest segment instead of requiring an exact zero.
        a,b=max(itertools.combinations(vertices,2),key=lambda pair:(points[pair[1]]-points[pair[0]]).length())
        axis=points[b]-points[a];length=axis.length();tolerance=max(length*1e-6,1e-7)
        if length>1e-8 and any(((points[v]-points[a])^axis).length()/length>tolerance for v in vertices):
            continue
        bad.append(component)
        for a,b in zip(vertices,vertices[1:]+vertices[:1]):
            direction=points[b]-points[a];squared=direction*direction
            if squared<1e-16:continue
            for vertex in vertices:
                if vertex in (a,b):continue
                t=((points[vertex]-points[a])*direction)/squared
                if 1e-6<t<1-1e-6 and (points[vertex]-(points[a]+direction*t)).length()<tolerance:
                    splits.append((om.MPoint(points[a]),om.MPoint(points[b]),om.MPoint(points[vertex])))
    if not bad:return False
    cmds.delete(bad)
    for first,last,target in splits:
        fn=_mesh_fn(_MESH);points=fn.getPoints(om.MSpace.kWorld)
        matches=[v for v,p in enumerate(points) if (p-target).length()<1e-6]
        if len(matches)!=1:continue
        source=matches[0];edge_iterator=om.MItMeshEdge(fn.dagPath());found=None
        span=last-first;span_squared=span*span
        for edge in range(fn.numEdges):
            a,b=fn.getEdgeVertices(edge)
            if source in (a,b):continue
            parameters=[((points[v]-first)*span)/span_squared for v in (a,b)]
            if any(not -1e-6<=t<=1+1e-6 or (points[v]-(first+span*t)).length()>1e-6
                   for v,t in zip((a,b),parameters)):
                continue
            direction=points[b]-points[a]
            if direction*direction<1e-16:continue
            t=((target-points[a])*direction)/(direction*direction)
            if not 1e-6<t<1-1e-6:continue
            edge_iterator.setIndex(edge)
            if len(edge_iterator.getConnectedFaces())==1:
                found=edge,t
                break
        if found is None:continue
        count=fn.numVertices
        # A single insertpoint adds a vertex to the edge without splitting
        # its parent face. Maya interpolates that face's UVs itself.
        cmds.polySplit(_MESH,insertpoint=[found],constructionHistory=False)
        fresh=_mesh_fn(_MESH)
        if fresh.numVertices!=count+1:
            raise ValueError("Не удалось соединить частичный разрез на восстановленном ребре.")
        cmds.move(target.x,target.y,target.z,_MESH+f".vtx[{count}]",absolute=True,worldSpace=True)
        cmds.polyMergeVertex([_MESH+f".vtx[{source}]",_MESH+f".vtx[{count}]"],distance=.001,
                            alwaysMergeTwoVertices=True,constructionHistory=False)
    return True


def _dissolve_fan_roots(points):
    """Remove only obsolete fan tips on straight edges, keeping cuts and UVs."""
    for point in points:
        fn = _mesh_fn(_MESH)
        vertices = fn.getPoints(om.MSpace.kWorld)
        matches = [i for i,p in enumerate(vertices) if (p-point).length()<1e-6]
        if len(matches)!=1:
            continue
        index = matches[0]
        iterator = om.MItMeshVertex(fn.dagPath());iterator.setIndex(index)
        neighbors = list(iterator.getConnectedVertices())
        if len(neighbors)!=2 or len(iterator.getConnectedFaces())!=2:
            continue
        a,b = neighbors
        first,second = vertices[a]-vertices[index],vertices[b]-vertices[index]
        if min(first.length(),second.length())<1e-8 or first.normal()*second.normal()>-1+1e-8:
            continue
        blend = first.length()/(first.length()+second.length())
        preserve = False
        for uv_set in fn.getUVSetNames():
            counts,_ = fn.getAssignedUVs(uv_set)
            for face in iterator.getConnectedFaces():
                if not counts[face]:
                    continue
                polygon = list(fn.getPolygonVertices(face))
                try:
                    uv = fn.getPolygonUV(face,polygon.index(index),uv_set)
                    ua = fn.getPolygonUV(face,polygon.index(a),uv_set)
                    ub = fn.getPolygonUV(face,polygon.index(b),uv_set)
                    if any(abs(uv[i]-(ua[i]*(1-blend)+ub[i]*blend))>1e-5 for i in range(2)):
                        preserve = True
                except (RuntimeError,ValueError):
                    preserve = True
        if not preserve:
            cmds.polyDelVertex(_MESH+f".vtx[{index}]",constructionHistory=False)


def _prepare_faces(backend, faces):
    """Feed recognized face strips to the vendor's existing drag operations."""
    global _SELECTION,_FACE_DETAILS
    from .search_cache import prepare
    _FACE_DETAILS = {}
    centers, rows, weights = prepare(_MESH, [int(face.rsplit("[",1)[1][:-1]) for face in faces],details=_FACE_DETAILS)
    points = _mesh_fn(_MESH).getPoints(om.MSpace.kWorld)
    backend.ppData = [list(center)[:3] for center in centers]
    backend.vLData = [[_MESH+f".vtx[{v}]" for v in row] for row in rows]
    backend.cLData = [[[(center[i]-points[v][i])/100 for i in range(3)] for v in row]
                      for center, row in zip(centers,rows)]
    backend.cumulative_fractions = weights
    backend.storeUniBevelCountA = backend.storeUniBevelCountB = 100
    backend.ctx = "unBevelCtx"
    _SELECTION = faces
    backend.mc.draggerContext(backend.ctx,cursor="crossHair")
    cmds.setToolTo(CONTEXT)


def _load():
    global _BACKEND,_STAMP
    stamp = (SCRIPT_PATH.stat().st_mtime_ns,SCRIPT_PATH.stat().st_size)
    if _BACKEND is not None and stamp==_STAMP:
        return _BACKEND
    tree = ast.parse(SCRIPT_PATH.read_text(encoding="utf-8-sig"),filename=str(SCRIPT_PATH))
    tail = tree.body[-1]
    if not (isinstance(tail,ast.Expr) and isinstance(tail.value,ast.Call)
            and isinstance(tail.value.func,ast.Name) and tail.value.func.id=="unBevel"
            and not tail.value.args and not tail.value.keywords):
        raise ValueError("В unBevel1.54.py не найден ожидаемый вызов запуска unBevel().")
    tree.body.pop()
    backend = types.ModuleType("model_pack_bevel._unbevel_vendor")
    backend.__file__ = str(SCRIPT_PATH)
    exec(compile(tree,str(SCRIPT_PATH),"exec"),backend.__dict__)
    backend.mc = _Commands()
    backend.mel = _Mel()
    _BACKEND,_STAMP = backend,stamp
    return backend


def active():
    return _RUNNING and cmds.currentCtx()==CONTEXT


def remove_selected(*, face_maps=None):
    """Collapse selected bevels; optionally return old-to-new polygon IDs.

    Maps are published only after a successful transaction. UI group masks
    can then follow native merges without reclassifying the edited geometry.
    """
    global _FACE_MAPS
    previous=_FACE_MAPS;maps={} if face_maps is not None else None
    _FACE_MAPS=maps
    try:
        result=_remove_selected()
        if result and face_maps is not None:face_maps.update(maps)
        return result
    finally:_FACE_MAPS=previous


def _remove_selected():
    """Fully collapse the selected bevels as a single undoable action."""
    global _FINISHING
    cmds.setToolTo("selectSuperContext")
    if (cmds.filterExpand(expand=True,selectionMask=34) and
            not cmds.filterExpand(expand=True,selectionMask=32)):
        return _remove_face_batches()
    # Activation preflights geometry before recording any destructive step
    # and also commits an outgoing Bevel preview.
    if _RUNNING:
        shutdown()
    if not activate():
        return False
    mesh=_MESH;_FINISHING=True
    failed=None;changed=False
    cmds.undoInfo(openChunk=True,chunkName="UnBevel")
    try:
        for center,vertices in zip(_BACKEND.ppData,_BACKEND.vLData):
            cmds.scale(0,0,0,vertices,componentSpace=True,relative=True,pivot=center)
            changed=True
        _BACKEND.unBevelOff()
    except Exception as exc:
        failed=exc
    finally:
        cmds.undoInfo(closeChunk=True)
        with without_undo():
            cmds.setToolTo("selectSuperContext")
        _cleanup()
        _FINISHING=False
    if failed is not None:
        if changed:
            cmds.undo()
        cmds.warning("UnBevel: "+str(failed))
        return False
    if mesh and cmds.objExists(mesh):
        with without_undo():
            _component_selection(mesh)
    return True


def _face_job(mesh, selected):
    """Prefer restoring a complete selection; otherwise remove a proven band."""
    from . import unbevel_faces,bevel_detection
    try:
        from .search_cache import prepare
        prepare(mesh,selected)
        return set(selected)
    except ValueError as error:failure=str(error)
    groups=[g for g in bevel_detection.detect(mesh).groups if g.ready and set(g.faces)<=selected]
    if not groups or not selected<=set().union(*(set(g.faces) for g in groups)):
        raise ValueError(failure)
    ranked=[]
    for group in groups:
        _,_,weights=prepare(mesh,group.faces)
        ranked.append(((group.source!='history',abs(weights[-1]-1)<1e-8,-len(group.faces)),group))
    return set(min(ranked,key=lambda entry:entry[0])[1].faces)


def _remove_face_batches():
    """Remap remaining face IDs after each band, keeping one undo for all meshes."""
    global _FINISHING,_SELECTION
    original=om.MGlobal.getActiveSelectionList();targets={};jobs={}
    try:
        faces=cmds.filterExpand(expand=True,selectionMask=34) or []
        for component in cmds.ls(faces,flatten=True,long=True) or []:
            mesh=_mesh_fn(component.split('.')[0]).fullPathName()
            targets.setdefault(mesh,set()).add(int(component.rsplit('[',1)[1][:-1]))
        # No geometry or selection changes before every mesh has a first
        # valid job. A later failure rolls back the entire native undo chunk.
        jobs={mesh:_face_job(mesh,selected) for mesh,selected in targets.items()}
    except (ValueError,RuntimeError) as error:
        cmds.warning('UnBevel: '+str(error));return False
    changed=False;failed=None
    cmds.undoInfo(openChunk=True,chunkName='UnBevel')
    try:
        for mesh,selected in targets.items():
            remaining=set(selected);job=jobs[mesh]
            while remaining:
                cmds.select([mesh+f'.f[{f}]' for f in sorted(job)],replace=True);changed=True
                if not activate():raise ValueError('Не удалось подготовить отдельную фаску к удалению.')
                _FINISHING=True
                # Keep every pending polygon in the existing merge remapper;
                # native merge renumbers faces after the first band is gone.
                _SELECTION=[mesh+f'.f[{f}]' for f in sorted(remaining)]
                for center,vertices in zip(_BACKEND.ppData,_BACKEND.vLData):
                    cmds.scale(0,0,0,vertices,componentSpace=True,relative=True,pivot=center)
                _BACKEND.unBevelOff()
                pending=cmds.filterExpand(expand=True,selectionMask=34) or []
                next_remaining={int(f.rsplit('[',1)[1][:-1]) for f in pending}
                if len(next_remaining)>=len(remaining):raise ValueError('Удаление фаски не изменило выделенную полосу.')
                remaining=next_remaining
                _cleanup();_FINISHING=False
                if remaining:job=_face_job(mesh,remaining)
    except Exception as error:failed=error
    finally:
        cmds.undoInfo(closeChunk=True)
        with without_undo():cmds.setToolTo('selectSuperContext')
        _cleanup();_FINISHING=False
    if failed is not None:
        if changed:cmds.undo()
        with without_undo():om.MGlobal.setActiveSelectionList(original)
        cmds.warning('UnBevel: '+str(failed));return False
    with without_undo():
        cmds.selectMode(component=True);cmds.selectType(allComponents=False);cmds.selectType(polymeshFace=True)
        cmds.hilite(list(targets),replace=True)
    return True


def _free_name(prefix,hud=False):
    name,index = prefix,0
    def exists(value):
        return cmds.headsUpDisplay(value,exists=True) if hud else cmds.objExists(value)
    while exists(name):
        index += 1
        name = prefix+str(index)
    return name


def _validate(backend):
    if not backend.ppData or not (len(backend.ppData)==len(backend.vLData)==len(backend.cLData)):
        raise ValueError("Не удалось распознать сечения фаски.")
    if len(backend.cumulative_fractions)<len(backend.ppData):
        raise ValueError("Выделение не образует согласованную полосу фаски.")
    for point,vertices,vectors in zip(backend.ppData,backend.vLData,backend.cLData):
        if (not vertices or len(vertices)!=len(vectors) or not all(math.isfinite(v) for v in point)
                or not all(math.isfinite(v) for vector in vectors for v in vector)):
            raise ValueError("У фаски некорректное или вырожденное сечение.")


def activate():
    """Toggle the original tool, after finishing the outgoing Model Pack tool."""
    global _RUNNING,_PRESSED,_MESH,_NAMES,_INPUT_MODE
    if active():
        shutdown()
        return False
    if _RUNNING:
        shutdown()
    selection = None
    try:
        backend = _load()
        # Finish a pending Bevel while its undo recording is still enabled.
        # Its finalize callback also removes numeric fields from the toolbox.
        cmds.setToolTo("selectSuperContext")
        if cmds.draggerContext(CONTEXT,exists=True):
            cmds.deleteUI(CONTEXT)
        edges = cmds.filterExpand(expand=True,selectionMask=32) or []
        faces = cmds.filterExpand(expand=True,selectionMask=34) or []
        if edges and faces:
            raise ValueError("Выделите полигоны полосы фаски или её поперечные рёбра отдельно.")
        if not edges and not faces:
            raise ValueError("Выделите полигоны полосы фаски, треугольный веер на торце или поперечные рёбра и включите UnBevel.")
        components = cmds.ls(faces or edges,flatten=True,long=True)
        meshes = {component.split(".")[0] for component in components}
        if len(meshes)!=1:
            raise ValueError("UnBevel работает с фасками одного меша за запуск.")
        _MESH = meshes.pop()
        _INPUT_MODE = "face" if faces else "edge"
        selection = om.MGlobal.getActiveSelectionList()
        _NAMES = {"saveSel":_free_name("modelPackUnBevelSelection"),
                  "HUDunBevelStep":_free_name("modelPackUnBevelHUD",hud=True),
                  "unBevelCtx":CONTEXT}
        _RUNNING,_PRESSED = True,False
        with without_undo():
            cmds.select(components,replace=True)
            if faces:
                _prepare_faces(backend,components)
            else:
                backend.unBevel()
            _validate(backend)
            _component_selection(_MESH)
        return True
    except Exception as exc:
        shutdown()
        if selection is not None:
            with without_undo():
                om.MGlobal.setActiveSelectionList(selection)
        detail = ("Выделите несколько поперечных рёбер фаски вместе с крайними рёбрами "
                  "соседних поверхностей." if isinstance(exc,(IndexError,ZeroDivisionError)) else str(exc))
        cmds.warning("UnBevel: "+detail)
        return False


def _press(*args):
    global _PRESSED
    if not _RUNNING:
        return
    try:
        if cmds.draggerContext(CONTEXT,q=True,button=True)!=1:
            return
        _BACKEND.unBevelPress()
        _PRESSED = True
    except Exception as exc:
        cmds.warning("UnBevel: "+str(exc))
        shutdown()


def _drag(*args):
    if not _RUNNING or not _PRESSED:
        return
    try:
        _BACKEND.unBevelDrag()
        # The supplied Ctrl falloff loop omits its last section. Correct this
        # for recognized face strips, where each patch has explicit endpoints.
        if _INPUT_MODE == "face" and cmds.getModifiers() == 4 and _BACKEND.lockCount > 0:
            i = len(_BACKEND.ppData)-1
            factor = (_BACKEND.lockCount + (_BACKEND.storeUniBevelCountA-_BACKEND.lockCount)
                      * _BACKEND.cumulative_fractions[i])
            for vertex,vector in zip(_BACKEND.vLData[i],_BACKEND.cLData[i]):
                point = [p-v*factor for p,v in zip(_BACKEND.ppData[i],vector)]
                cmds.move(*point,vertex,absolute=True,ws=True)
    except Exception as exc:
        cmds.warning("UnBevel: "+str(exc))
        shutdown()


def _cleanup():
    global _RUNNING,_PRESSED,_MESH,_NAMES,_SELECTION,_FACE_DETAILS
    with without_undo():
        hud = _NAMES.get("HUDunBevelStep")
        if hud and cmds.headsUpDisplay(hud,exists=True):
            cmds.headsUpDisplay(hud,remove=True)
    _RUNNING,_PRESSED,_MESH = False,False,None
    _NAMES,_SELECTION = {},None
    _FACE_DETAILS = {}


def _release(*args):
    global _FINISHING
    if not _RUNNING or not _PRESSED:
        return
    _FINISHING = True
    try:
        _BACKEND.unBevelOff()
    except Exception as exc:
        cmds.warning("UnBevel: "+str(exc))
        cmds.setToolTo("selectSuperContext")
    finally:
        mesh = _MESH
        _cleanup()
        if mesh and cmds.objExists(mesh):
            with without_undo():
                _component_selection(mesh)
        _FINISHING = False


def _finalize(*args):
    # A new native tool has already become current; leave it active. Original
    # UnBevel applies geometry immediately, so retain it and clear only helpers.
    if not _FINISHING:
        _cleanup()


def shutdown():
    if active():
        cmds.setToolTo("selectSuperContext")
    _cleanup()
