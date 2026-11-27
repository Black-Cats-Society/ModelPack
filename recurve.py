"""Recurve for Maya 2025. Load as a Python plug-in, then run recurve.install()."""
import math
import sys
import bisect
import os
import json
import traceback
from collections import deque
import maya.cmds as cmds
import maya.api.OpenMaya as om
import maya.OpenMayaUI as omui
_previous_version = globals().get('VERSION')
VERSION = '0.8.5'
MODEL_PACK_BUILD = '0.1.3'
VERTEX_COLOR_SUPPORT = True
ERROR_PATH = None
try:
    from PySide6 import QtCore, QtWidgets, QtGui
    from shiboken6 import wrapInstance
except ImportError:
    from PySide2 import QtCore, QtWidgets, QtGui
    from shiboken2 import wrapInstance

if globals().get('SESSION') is not None:
    SESSION.finish(False)
if globals().get('LISTENER') is not None:
    QtWidgets.QApplication.instance().removeEventFilter(LISTENER)
    LISTENER.deleteLater()
SESSION = None
PENDING = None
LISTENER = None
IDLE_TASKS = deque()
IDLE_SCHEDULED = False
MENU = None
# An embedding toolbox can supply its own panel while keeping these shortcuts.
MENU_HOST = None
INPUT_GUARD = None
CONNECT_NEW = False
PANEL_STYLE = globals().get('PANEL_STYLE','Graphite')
PANEL_LAYOUT = globals().get('PANEL_LAYOUT','Mini Tower') if _previous_version == VERSION else 'Mini Tower'
PENDING_WHEEL_DELTA = 0
PENDING_WHEEL_VIEW = None
PENDING_CONTROL_DELTA = 0
PENDING_CONTROL_VIEW = None
if globals().get('SCENE_CALLBACKS'):
    om.MMessage.removeCallbacks(SCENE_CALLBACKS)
SCENE_CALLBACKS = []


def record_error(stage,exc):
    """Keep the actual failing selection and traceback for reproducible reports."""
    report={'version':VERSION,'model_pack_build':MODEL_PACK_BUILD,'stage':stage,'error':str(exc),
            'traceback':''.join(traceback.format_exception(type(exc),exc,exc.__traceback__)),
            'modules':{name:getattr(sys.modules.get(name),'__file__',None)
                       for name in ('recurve','recurve_strip','recurve_bulk','recurve_uv','recurve_colors','recurve_normals','recurve_controls','recurve_faces','recurve_panel')}}
    try:
        report['selection']=cmds.ls(selection=True,long=True) or []
        try:report['plugin_path']=cmds.pluginInfo('recurve',query=True,path=True)
        except RuntimeError:report['plugin_path']=None
        if SESSION is not None:
            report['session']={'path':SESSION.path,'count':SESSION.count,
                               'connect':SESSION.connect,'chains':SESSION.chains}
            fn=om.MFnMesh(SESSION.before)
        else:
            selected=om.MGlobal.getActiveSelectionList()
            path=selected.getDagPath(0)
            if path.node().hasFn(om.MFn.kTransform):path.extendToShape()
            fn=om.MFnMesh(path)
        report['mesh']={'vertices':fn.numVertices,'faces':fn.numPolygons,
                        'uv_sets':list(fn.getUVSetNames())}
        if fn.numVertices<=25000:
            counts,vertices=fn.getVertices()
            report['geometry']={'points':[list(p)[:3] for p in fn.getPoints()],
                                'face_counts':list(counts),'face_vertices':list(vertices)}
    except Exception as detail_error:
        report['capture_error']=str(detail_error)
    try:
        target=ERROR_PATH or os.path.join(os.path.dirname(os.path.abspath(record_error.__code__.co_filename)),
                                         'Recurve_last_error.json')
        with open(target,'w',encoding='utf-8') as stream:
            json.dump(report,stream,ensure_ascii=False,indent=2)
        print('RECURVE ERROR DETAILS: '+target)
    except OSError:
        pass
    print(report['traceback'])


def queue_idle(callback):
    """Never edit Maya's DAG or DG while Qt is delivering an input event."""
    global IDLE_SCHEDULED
    IDLE_TASKS.append(callback)
    if not IDLE_SCHEDULED:
        IDLE_SCHEDULED = True
        if not cmds.about(batch=True):
            cmds.evalDeferred(run_idle,lowestPriority=True)


def run_idle():
    global IDLE_SCHEDULED
    IDLE_SCHEDULED = False
    while IDLE_TASKS:
        callback = IDLE_TASKS.popleft()
        try:
            callback()
        except Exception as exc:
            record_error('idle',exc)
            cmds.warning('Recurve: '+str(exc))
            if SESSION:
                SESSION.finish(False)
            IDLE_TASKS.clear()
            break


def mesh_plug(path,attribute):
    selection = om.MSelectionList()
    selection.add(path+'.'+attribute)
    return selection.getPlug(0)


def maya_useNewAPI():
    pass


def mesh(path):
    sel = om.MSelectionList()
    sel.add(path)
    return om.MFnMesh(sel.getDagPath(0))


def topology_issues(path):
    """Return Maya's actual non-manifold component names for this mesh."""
    return sorted(set((cmds.polyInfo(path,nonManifoldEdges=True) or []) +
                      (cmds.polyInfo(path,nonManifoldVertices=True) or [])))


def diagnose(select=False):
    """Print offending components; optionally select them for inspection."""
    selected = cmds.ls(selection=True,objectsOnly=True,long=True) or []
    shapes = set()
    for node in selected:
        if cmds.nodeType(node) == 'mesh':
            shapes.add(node)
        else:
            shapes.update(cmds.listRelatives(node,shapes=True,noIntermediate=True,
                                             fullPath=True,type='mesh') or [])
    issues = [component for shape in sorted(shapes) for component in topology_issues(shape)]
    for component in issues:
        print('Recurve non-manifold: '+component)
    if select and issues:
        cmds.select(issues,replace=True)
    if not issues:
        print('Recurve: no non-manifold components on the selected meshes.')
    return issues


def copy_mesh_data(source):
    """Copy geometry together with its component-set/material membership."""
    data = om.MFnMeshData().create()
    om.MFnMesh().copy(source, data)
    om.MFnMeshData(data).copyObjectGroups(source)
    return data


def snapshot(fn):
    source = (om.MFnDependencyNode(fn.object()).findPlug('outMesh',False).asMObject()
              if fn.object().hasFn(om.MFn.kDagNode) else fn.object())
    return copy_mesh_data(source)


def transfer_face_groups(source, data, owners):
    """Carry polygon set membership from each source face to its descendants."""
    groups = om.MFnMeshData(source)
    if not groups.objectGroupCount:
        return data
    output = om.MFnMeshData(data)
    output.copyObjectGroups(source)
    owners = list(owners)
    for index in range(groups.objectGroupCount):
        group = groups.objectGroup(index)
        component = groups.objectGroupComponent(group)
        if not component.hasFn(om.MFn.kMeshPolygonComponent):
            continue
        old = om.MFnSingleIndexedComponent(component)
        members = None if old.isComplete else set(old.getElements())
        target = om.MFnSingleIndexedComponent()
        component = target.create(om.MFn.kMeshPolygonComponent)
        target.addElements([face for face, owner in enumerate(owners) if members is None or owner in members])
        output.setObjectGroupComponent(group, component)
    return data


class SurfaceMaterial:
    """Reversible whole-shape link for a uniformly face-assigned material.

    Preserve existing face groups and their upstream groupParts nodes. A stable
    whole-object shading link covers newly generated polygons and survives scene
    reload; disconnecting this link restores the exact original assignment.
    """
    def __init__(self,path):
        self.modifier=None;self.active=False
        selection=om.MSelectionList();selection.add(path)
        dag=selection.getDagPath(0)
        shaders,indices=om.MFnMesh(dag).getConnectedShaders(dag.instanceNumber())
        used=set(indices)
        if len(used)!=1 or next(iter(used))<0:return
        shader=shaders[next(iter(used))]
        shape=om.MFnDependencyNode(dag.node())
        whole=shape.findPlug('instObjGroups',False).elementByLogicalIndex(dag.instanceNumber())
        outgoing=whole.connectedTo(False,True)
        if any(plug.node()==shader for plug in outgoing):return
        self.modifier=om.MDGModifier()
        for destination in outgoing:
            if destination.node().hasFn(om.MFn.kShadingEngine):self.modifier.disconnect(whole,destination)
        groups=whole.child(0)
        for index in groups.getExistingArrayAttributeIndices():
            member=groups.elementByLogicalIndex(index)
            for destination in member.connectedTo(False,True):
                if destination.node().hasFn(om.MFn.kShadingEngine):self.modifier.disconnect(member,destination)
        members=om.MFnDependencyNode(shader).findPlug('dagSetMembers',False)
        occupied=set(members.getExistingArrayAttributeIndices())
        index=max(occupied,default=-1)+1
        self.modifier.connect(whole,members.elementByLogicalIndex(index))

    def redo(self):
        if self.modifier is not None and not self.active:
            self.modifier.doIt();self.active=True

    def undo(self):
        if self.modifier is not None and self.active:
            self.modifier.undoIt();self.active=False


class FaceMaterialParts:
    """Serialize per-face shading with Maya's native groupParts nodes."""

    def __init__(self, dag):
        self.group_ids = {}
        self.parts = {}
        shape = om.MFnDependencyNode(dag.node())
        array = shape.findPlug('instObjGroups', False).elementByLogicalIndex(dag.instanceNumber()).child(0)
        for index in array.getExistingArrayAttributeIndices():
            member = array.elementByLogicalIndex(index)
            if any(plug.node().hasFn(om.MFn.kShadingEngine) for plug in member.connectedTo(False, True)):
                group = member.child(shape.attribute('objectGroupId'))
                self.group_ids[group.asInt()] = group

    def connect(self, modifier, source):
        for group, original in self.group_ids.items():
            node = om.MDGModifier.createNode(modifier, 'groupParts')
            modifier.renameNode(node, 'recurveMaterialParts#')
            parts = om.MFnDependencyNode(node)
            group_id = parts.findPlug('groupId', False)
            if original.isDestination:
                modifier.connect(original.source(), group_id)
            else:
                modifier.newPlugValueInt(group_id, group)
            modifier.connect(source, parts.findPlug('inputGeometry', False))
            self.parts[group] = parts.findPlug('inputComponents', False)
            source = parts.findPlug('outputGeometry', False)
        return source

    def reuse(self, source):
        parts = {}
        while not source.isNull:
            node = om.MFnDependencyNode(source.node())
            if node.typeName != 'groupParts' or not node.name().split(':')[-1].startswith('recurveMaterialParts'):
                break
            parts[node.findPlug('groupId', False).asInt()] = node.findPlug('inputComponents', False)
            source = node.findPlug('inputGeometry', False).source()
        if not source.isNull and set(parts) == set(self.group_ids):
            node = om.MFnDependencyNode(source.node())
            if node.typeName == 'mesh' and node.name().split(':')[-1].startswith('recurveSource'):
                self.parts = parts
                return source.node()
        return None

    def write(self, modifier, data):
        if not self.parts:
            return
        groups = om.MFnMeshData(data)
        for group, plug in self.parts.items():
            components = om.MFnComponentListData()
            value = components.create()
            if groups.hasObjectGroup(group):
                component = groups.objectGroupComponent(group)
                if component.hasFn(om.MFn.kMeshPolygonComponent):
                    components.add(component)
            modifier.newPlugValue(plug, value)


def ordered(fn, edges):
    graph = {}
    for e in edges:
        a, b = fn.getEdgeVertices(e)
        graph.setdefault(a, []).append(b)
        graph.setdefault(b, []).append(a)
    ends = sorted(v for v, n in graph.items() if len(n) == 1)
    closed = not ends and graph and all(len(n)==2 for n in graph.values())
    if (len(ends) != 2 and not closed) or any(len(n) > 2 for n in graph.values()):
        raise ValueError('Select one edge chain or ring without branches.')
    chain, previous, current = [], None, min(graph) if closed else ends[0]
    while True:
        chain.append(current)
        nxt = [v for v in graph[current] if v != previous]
        if not nxt:
            break
        previous, current = current, nxt[0]
        if closed and current==chain[0]:
            chain.append(current)
            break
    if len(chain)-(1 if closed else 0) != len(graph):
        raise ValueError('Select one connected chain.')
    return chain


def interpolate(points, count):
    """Catmull-Rom, sampled densely then redistributed by arc length."""
    dense = [points[0]]
    for i in range(len(points)-1):
        p0, p1 = points[max(0, i-1)], points[i]
        p2, p3 = points[i+1], points[min(len(points)-1, i+2)]
        for j in range(1, 33):
            t = j / 32.0
            dense.append(tuple(0.5*((2*b)+(-a+c)*t+
                         (2*a-5*b+4*c-d)*t*t+
                         (-a+3*b-3*c+d)*t*t*t)
                         for a,b,c,d in zip(p0,p1,p2,p3)))
    lengths = [0.0]
    for a,b in zip(dense,dense[1:]):
        lengths.append(lengths[-1]+math.dist(a,b))
    if lengths[-1] < 1e-10:
        raise ValueError('The selected chain has zero length.')
    result = []
    for i in range(count+1):
        s = lengths[-1]*i/count
        k = min(len(dense)-2, max(0,bisect.bisect_right(lengths,s)-1))
        w = (s-lengths[k])/max(1e-15,lengths[k+1]-lengths[k])
        result.append(tuple(a+(b-a)*w for a,b in zip(dense[k],dense[k+1])))
    return result


def triangulate_corners(corners,points):
    """Ear clipping in the face's dominant plane, retaining UV corner records."""
    xyz = [points[c[0]] for c in corners]
    normal = [sum((a[(i+1)%3]-b[(i+1)%3])*(a[(i+2)%3]+b[(i+2)%3])
                  for a,b in zip(xyz,xyz[1:]+xyz[:1])) for i in range(3)]
    axis = max(range(3),key=lambda i:abs(normal[i]))
    axes = [i for i in range(3) if i!=axis]
    xy = [(p[axes[0]],p[axes[1]]) for p in xyz]
    area = sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(xy,xy[1:]+xy[:1]))
    scale = max(max(p[i] for p in xy)-min(p[i] for p in xy) for i in (0,1))
    eps = max(1e-20,scale*scale*1e-12)
    if abs(area)<eps:
        raise ValueError('Connecting this face would create zero-area triangles.')
    sign = 1 if area>0 else -1
    def cross(a,b,c):
        return sign*((b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]))
    remaining = list(range(len(corners)))
    result = []
    while len(remaining)>3:
        for j,b in enumerate(remaining):
            a,c = remaining[j-1],remaining[(j+1)%len(remaining)]
            if cross(xy[a],xy[b],xy[c])<=eps:
                continue
            if any(cross(xy[a],xy[b],xy[k])>=-eps and
                   cross(xy[b],xy[c],xy[k])>=-eps and
                   cross(xy[c],xy[a],xy[k])>=-eps
                   for k in remaining if k not in (a,b,c)):
                continue
            result.append([corners[a],corners[b],corners[c]])
            remaining.pop(j)
            break
        else:
            raise ValueError('Cannot connect this face without invalid triangles. Try N-gons mode.')
    result.append([corners[k] for k in remaining])
    return result


def rebuild(source, chain, count, connect=False, strip=True):
    """Resample chain; collapse its old vertices and insert new face corners."""
    fn = om.MFnMesh(source)
    if count < 1 or count > 1000:
        raise ValueError('Segment count must be between 1 and 1000.')
    if count == len(chain)-1:
        return source,list(chain)
    if strip:
        from recurve_strip import rebuild_strip
        result = rebuild_strip(source,chain,count,connect,triangulate_corners)
        if result is not None:
            return result
    if chain[0]==chain[-1]:
        raise ValueError('This ring has no supported circular quad strip. Geometry was not changed.')
    if fn.numPolygons>20000:
        from recurve_bulk import rebuild_bulk
        return rebuild_bulk(source,chain,count,connect,interpolate,triangulate_corners)
    old = [tuple(p)[:3] for p in fn.getPoints()]
    curve = [old[v] for v in chain]
    samples = interpolate(curve, count)
    # Topological correspondence uses source chord length; endpoints stay fixed.
    lengths = [0.0]
    for a,b in zip(curve,curve[1:]):
        lengths.append(lengths[-1]+math.dist(a,b))
    if any(b-a < 1e-10 for a,b in zip(lengths,lengths[1:])):
        raise ValueError('Chain contains a zero-length edge.')
    params = [s/lengths[-1]*count for s in lengths]
    buckets = [int(math.floor(s+0.5)) for s in params]
    excluded = set(chain)
    points = [p for i,p in enumerate(old) if i not in excluded]
    mapping = {v:i for i,v in enumerate(v for v in range(len(old)) if v not in excluded)}
    base = len(points)
    points.extend(samples)
    mapping.update({v:base+k for v,k in zip(chain,buckets)})
    spans = {}
    for i,(a,b) in enumerate(zip(chain,chain[1:])):
        spans[a,b] = (i,False)
        spans[b,a] = (i,True)
    uvsets = fn.getUVSetNames()
    uvout = {name:[] for name in uvsets}
    uvcoords = {name:[list(values) for values in fn.getUVs(name)] for name in uvsets}
    uvcache = {name:{} for name in uvsets}
    from recurve_normals import NormalTransfer
    normal_transfer = NormalTransfer(fn,points)
    from recurve_colors import ColorTransfer
    color_transfer = ColorTransfer(fn)
    faces = []
    owners = []
    for f in range(fn.numPolygons):
        verts = list(fn.getPolygonVertices(f))
        corners = []
        for j,a in enumerate(verts):
            b = verts[(j+1)%len(verts)]
            corners.append((mapping[a], j, j, 0.0))
            if (a,b) in spans:
                i, reverse = spans[a,b]
                ks = list(range(buckets[i]+1,buckets[i+1]))
                if reverse:
                    ks.reverse()
                for k in ks:
                    w = (k-params[i])/(params[i+1]-params[i])
                    if reverse:
                        w = 1-w
                    corners.append((base+k,j,(j+1)%len(verts),max(0,min(1,w))))
        clean = []
        for c in corners:
            if not clean or c[0] != clean[-1][0]:
                clean.append(c)
        if len(clean)>1 and clean[0][0] == clean[-1][0]:
            clean.pop()
        if len(clean)<3:
            # Edge collapse can turn a neighbouring triangle into a line.
            # Remove only that collapsed face, as native collapse tools do.
            continue
        if len(set(c[0] for c in clean)) != len(clean):
            raise ValueError('This count would create a pinched face.')
        split = connect and len(clean)>3 and excluded.intersection(verts) and (len(clean)>4 or any(c[1]!=c[2] for c in clean))
        pieces = triangulate_corners(clean,points) if split else [clean]
        for piece in pieces:
            faces.append([c[0] for c in piece])
            owners.append(f)
            for vertex,a,b,w in piece:
                normal_transfer.add(len(faces)-1,vertex,f,a,b,w)
                color_transfer.add(f,a,b,w)
            for name in uvsets:
                values = []
                try:
                    for _,a,b,w in piece:
                        ia = fn.getPolygonUVid(f,a,name)
                        ib = fn.getPolygonUVid(f,b,name)
                        if a == b or w <= 0:
                            values.append(ia)
                        elif w >= 1:
                            values.append(ib)
                        else:
                            key = (ia,ib,round(w,12)) if ia<=ib else (ib,ia,round(1-w,12))
                            if key not in uvcache[name]:
                                us,vs = uvcoords[name]
                                uvcache[name][key] = len(us)
                                us.append(us[ia]+(us[ib]-us[ia])*w)
                                vs.append(vs[ia]+(vs[ib]-vs[ia])*w)
                            values.append(uvcache[name][key])
                except RuntimeError:
                    values = []
                uvout[name].append(values)
    data = om.MFnMeshData().create()
    out = om.MFnMesh()
    out.create([om.MPoint(*p) for p in points], [len(f) for f in faces],
               [v for f in faces for v in f], parent=data)
    # Maya must not silently split or repair source topology outside this operation.
    if out.numVertices != len(points) or any(list(out.getPolygonVertices(i))!=f for i,f in enumerate(faces)):
        raise ValueError('Maya would alter unrelated topology at this count.')
    smooth = {}
    for e in range(fn.numEdges):
        a,b = fn.getEdgeVertices(e)
        smooth[frozenset((mapping[a],mapping[b]))] = fn.isEdgeSmooth(e)
        if (a,b) in spans:
            i,_ = spans[a,b]
            for k in range(buckets[i],buckets[i+1]):
                smooth[frozenset((base+k,base+k+1))] = fn.isEdgeSmooth(e)
    out.setEdgeSmoothings(list(range(out.numEdges)),
                         [smooth.get(frozenset(out.getEdgeVertices(e)),True) for e in range(out.numEdges)])
    out.cleanupEdgeSmoothing()
    normal_transfer.apply(out)
    from recurve_uv import assign_uv_sets
    data=assign_uv_sets(data,{name:(*uvcoords[name],[len(f) for f in faceuvs],
                                    [v for f in faceuvs for v in f]) for name,faceuvs in uvout.items()},
                        fn.currentUVSetName())
    data=transfer_face_groups(source,color_transfer.apply(data),owners)
    out=om.MFnMesh(data)
    # Ensure the resampled path remains a single chain in the resulting mesh.
    pairs = {frozenset(out.getEdgeVertices(e)) for e in range(out.numEdges)}
    if any(frozenset((base+i,base+i+1)) not in pairs for i in range(count)):
        raise ValueError('Topology cannot represent this segment count.')
    return data, list(range(base,base+count+1))


def selected_chains(fn,edges):
    """Separate disconnected edge curves, preserving each curve's topology."""
    graph={}
    for edge in edges:
        a,b=fn.getEdgeVertices(edge)
        graph.setdefault(a,[]).append((b,edge));graph.setdefault(b,[]).append((a,edge))
    chains=[]
    while graph:
        seed=next(iter(graph));vertices={seed};todo=[seed];part=set()
        while todo:
            for other,edge in graph[todo.pop()]:
                part.add(edge)
                if other not in vertices:vertices.add(other);todo.append(other)
        for vertex in vertices:graph.pop(vertex)
        chains.append(ordered(fn,sorted(part)))
    if not chains:raise ValueError('Select polygon edge chains first.')
    return chains


def rebuild_chains(source,chains,count,connect):
    if len(chains)==1 and om.MFnMesh(source).numPolygons>20000:
        data,ids=rebuild(source,chains[0],count,connect=connect)
        return data,[ids]
    from recurve_strip import rebuild_group
    data,ids=rebuild_group(source,chains,count,connect,triangulate_corners,interpolate)
    return data,ids


class MeshSource:
    """Standard Maya mesh history source; rendered shape is never replaced in place."""
    def __init__(self,path,before,face_materials=False):
        self.path,self.before = path,before
        self.material=SurfaceMaterial(path)
        self.tweak_modifier = om.MDGModifier()
        self.cached_before = None
        tweaks = mesh_plug(path,'pnts')
        for index in tweaks.getExistingArrayAttributeIndices():
            element = tweaks.elementByLogicalIndex(index)
            for axis in range(3):
                plug = element.child(axis)
                animated = plug.isDestination
                if animated:
                    for source in plug.connectedTo(True,False):self.tweak_modifier.disconnect(source,plug)
                if animated or plug.asDouble()!=0:
                    if plug.isLocked:raise ValueError('Vertex tweaks are locked.')
                    self.tweak_modifier.newPlugValueDouble(plug,0.0)
        selection = om.MSelectionList(); selection.add(path)
        dag = selection.getDagPath(0)
        destination = mesh_plug(path,'inMesh')
        incoming = destination.connectedTo(True,False)
        self.upstream = tuple(incoming)
        self.face_materials = None
        if face_materials:
            material_parts = FaceMaterialParts(dag)
            _, assignments = om.MFnMesh(dag).getConnectedShaders(dag.instanceNumber())
            if len(set(assignments)) > 1 or len(material_parts.group_ids) > 1:
                self.face_materials = material_parts
        reused = None
        if len(incoming) == 1:
            if self.face_materials is not None:
                reused = self.face_materials.reuse(incoming[0])
            elif om.MFnDependencyNode(incoming[0].node()).name().split(':')[-1].startswith('recurveSource'):
                reused = incoming[0].node()
        if reused is not None:
            self.node = reused
            self.output = incoming[0]
            self.before = snapshot(om.MFnMesh(self.node))
            if self.face_materials is not None:
                om.MFnMeshData(self.before).copyObjectGroups(before)
            self.modifier = None
            self.connected = False
            self.redo()
            self.write(before)
            return
        if not incoming:
            try:
                cached = mesh_plug(path,'cachedInMesh').asMObject()
            except RuntimeError:
                # Untweaked static meshes may have no cachedInMesh value yet.
                cached = before
            if cached.isNull():cached=before
            self.cached_before = before if cached == before else copy_mesh_data(cached)
        parent = dag.transform()
        self.modifier = om.MDagModifier()
        self.node = self.modifier.createNode('mesh',parent)
        self.modifier.renameNode(self.node,'recurveSource#')
        node_fn = om.MFnDependencyNode(self.node)
        self.modifier.newPlugValueBool(node_fn.findPlug('intermediateObject',False),True)
        self.modifier.newPlugValue(node_fn.findPlug('cachedInMesh',False),before)
        for source in destination.connectedTo(True,False):
            self.modifier.disconnect(source,destination)
        self.output = node_fn.findPlug('outMesh',False)
        if self.face_materials is not None:
            self.output = self.face_materials.connect(self.modifier, self.output)
            self.face_materials.write(self.modifier, before)
        self.modifier.connect(self.output,destination)
        self.connected = False
        self.redo()

    def redo(self):
        if not self.connected:
            self.tweak_modifier.doIt()
            try:
                if self.modifier is not None:self.modifier.doIt()
            except Exception:
                self.tweak_modifier.undoIt()
                raise
            self.connected = True
            self.material.redo()

    def write(self,data,preserve_selection=False):
        if not om.MObjectHandle(self.node).isValid():
            raise RuntimeError('Recurve source no longer exists; operation cancelled.')
        if not preserve_selection:
            om.MGlobal.setActiveSelectionList(om.MSelectionList())
        plug = om.MFnDependencyNode(self.node).findPlug('cachedInMesh',False)
        modifier = om.MDGModifier()
        colored = bool(om.MFnMesh(data).numColorSets)
        # Node color writes must not mutate the saved Before/After data that
        # native Undo/Cancel still owns through Maya's shared mesh storage.
        modifier.newPlugValue(plug,copy_mesh_data(data) if colored else data)
        if self.face_materials is not None:
            self.face_materials.write(modifier, data)
        modifier.doIt()
        if colored:
            # Maya serializes colors from the source node's color-set plugs.
            # A cachedInMesh assignment alone displays them until scene reopen,
            # but does not populate those plugs on our new intermediate source.
            from recurve_colors import sync_source_colors
            sync_source_colors(self.node,data)
        # Force normal DG evaluation before selecting new component IDs.
        mesh_plug(self.path,'outMesh').asMObject()

    def undo(self):
        if self.connected:
            if not om.MObjectHandle(self.node).isValid():
                self.connected = False
                return
            self.write(self.before)
            if self.modifier is not None:
                self.modifier.undoIt()
                if self.cached_before is not None:
                    restore = om.MDGModifier()
                    restore.newPlugValue(mesh_plug(self.path,'cachedInMesh'),self.cached_before)
                    restore.doIt()
            self.tweak_modifier.undoIt()
            self.material.undo()
            self.connected = False


class Apply(om.MPxCommand):
    def doIt(self,args):
        self.path,before,self.after = PENDING[:3]
        self.selections = PENDING[3:5] if len(PENDING)>3 else None
        self.binding = PENDING[6] if len(PENDING)>6 else None
        if len(PENDING)>5:
            # Ordinary Apply adopts the displayed DG result. Save Curves
            # instead installs an independently evaluated scene history node.
            self.source = PENDING[5]
            if self.binding is not None:
                try:
                    self.source.write(self.binding.base,preserve_selection=True)
                    self.binding.redo()
                    self.binding.validate(self.path,self.after)
                except Exception:
                    self.binding.undo()
                    self.source.write(self.after,preserve_selection=True)
                    raise
            if self.selections:
                om.MGlobal.setActiveSelectionList(self.selections[1])
        else:
            self.source = MeshSource(self.path,before)
            self.redoIt()

    def redoIt(self):
        self.source.redo()
        if self.binding is not None:
            self.source.write(self.binding.base,preserve_selection=True)
            self.binding.redo()
        else:self.source.write(self.after)
        if self.selections:
            om.MGlobal.setActiveSelectionList(self.selections[1])

    def undoIt(self):
        if self.binding is not None:self.binding.undo()
        self.source.undo()
        if self.selections:
            om.MGlobal.setActiveSelectionList(self.selections[0])

    def isUndoable(self):
        return True

class Session(QtCore.QObject):
    preserves_initial_preview = True

    def __init__(self,path,chain,view,connect=None,face_region=None):
        super().__init__()
        self.face_region = face_region
        self.chains = chain if isinstance(chain[0],(list,tuple)) else [chain]
        self.path,self.chain,self.view = path,self.chains[0],view
        self.before = snapshot(mesh(path))
        self.after = self.before
        self.ids = [list(c) for c in self.chains]
        self.count = len(self.chain)-1
        self.changed = False
        self.connect = CONNECT_NEW if connect is None else connect
        self.wheel = 0
        self.controls = None
        self.align_to_curve = False
        self.influence_distance = 0.
        self.influence_edges = 0
        self.influence_by_edges = False
        self.influence_smooth = False
        self.cv_soft_mode = 0
        self.cv_soft_radius = None
        self.influence_falloff = ((0., 1.), (1., 0.))
        self.control_base = None
        self.control_ids = None
        self.profile_source = None
        self.profile_chains = None
        self.control_groups = set()
        self.selection = om.MGlobal.getActiveSelectionList()
        self.preview_shape = path
        self._source = None
        self.label = QtWidgets.QLabel(view)
        self.label.setStyleSheet('background:#252525;color:#f4d36c;padding:10px;font-size:14px;')
        self.label.move(20,20)
        self.message()
        self.label.setVisible(MENU is None or not MENU.isVisible())

    @property
    def source(self):
        # Opening the panel alone needs no intermediate shape or undo modifiers.
        if self._source is None:
            self._source = MeshSource(self.path, self.before, face_materials=True)
        return self._source

    def message(self,error=''):
        if self.face_region is not None:
            self.label.setText('Control Curve | %d faces | %d contours | Enter: apply | Esc: cancel\n%s' % (len(self.face_region.faces),len(self.chains),error))
        else:
            self.label.setText('Recurve | %d segments | %s | Wheel +/- | Enter: apply | Esc: cancel\n%s' % (self.count,'Triangles' if self.connect else 'N-gons',error))
        self.label.adjustSize()
        if MENU is not None:
            MENU.sync(self.count,error)
            if MENU.isVisible():self.label.hide()

    def result_changed(self,data,count):
        if self.controls is not None and self.controls.deformed:return True
        if any(count!=len(c)-1 for c in self.chains):return True
        if data==self.before:return False
        before,after=om.MFnMesh(self.before),om.MFnMesh(data)
        bc,bv=before.getVertices();ac,av=after.getVertices()
        return (before.numVertices!=after.numVertices or before.numPolygons!=after.numPolygons or
                list(bc)!=list(ac) or list(bv)!=list(av) or
                (self.profile_source is not None and any((a-b).length()>1e-8 for a,b in zip(before.getPoints(),after.getPoints()))))

    def adjust(self,delta):
        if self.face_region is not None:return
        self.wheel += delta
        steps = int(self.wheel/120)
        self.wheel -= steps*120
        target = max(3 if self.chain[0]==self.chain[-1] else 1,min(1000,self.count+steps))
        if target != self.count or (not self.changed and any(target!=len(c)-1 for c in self.chains)):
            try:
                self.rebuild_preview(target)
            except Exception as exc:
                record_error('adjust',exc)
                self.message(str(exc))

    def set_mode(self,connect):
        if self.face_region is not None:return
        if self.connect == connect:
            return
        self.connect = connect
        if self.changed:
            self.rebuild_preview(self.count)

    def rebuild_preview(self,count):
        if self.face_region is not None:
            raise ValueError('ReSegments requires edge chains. Face controls keep the original topology.')
        previous_count = self.count
        source = self.before if self.profile_source is None else self.profile_source
        chains = self.chains if self.profile_chains is None else self.profile_chains
        restore_original = (self.controls is None and self.profile_source is None
                            and all(count == len(chain) - 1 for chain in self.chains))
        if restore_original:
            if self._source is not None:
                self._source.undo()
                self._source = None
            data, ids = self.before, [list(chain) for chain in self.chains]
        else:
            data,ids = rebuild_chains(source,chains,count,self.connect)
            if self.controls is not None:
                self.control_base,self.control_ids = data,ids
                data = self.controls.deform(data,ids)
            self.source.write(data,preserve_selection=self.controls is not None)
        self.after,self.ids,self.count = data,ids,count
        self.changed = self.result_changed(data,count)
        if restore_original:
            om.MGlobal.setActiveSelectionList(self.selection)
        elif self.controls is None:
            om.MGlobal.setActiveSelectionList(chain_selection(self.path,data,ids))
        self.message()
        if MENU is not None and hasattr(MENU,'segment_changed'):
            MENU.segment_changed(self,previous_count,count)

    def create_controls(self,number=4):
        if self.controls is not None:
            self.controls.select()
            return
        if not isinstance(number,int) or not 3<=number<=32:
            raise ValueError('Control point count must be between 3 and 32.')
        from recurve_controls import ControlCurves, FaceControlCurves
        control_type = FaceControlCurves if self.face_region is not None else ControlCurves
        self.controls = control_type(self,number)
        self.control_groups.add(self.controls.group)
        self.control_base,self.control_ids = self.after,self.ids
        self.controls.timer.start()
        if self.cv_soft_mode:
            self.controls.set_soft_selection(self.cv_soft_mode, self.cv_soft_radius)
        if self.align_to_curve:self.update_controls()
        self.message()

    def set_align_to_curve(self,enabled):
        if self.face_region is not None:return
        enabled = bool(enabled)
        if self.align_to_curve == enabled:return
        self.align_to_curve = enabled
        if self.controls is not None:self.controls.flush()

    supports_edge_influence = True

    def set_cv_soft_selection(self, mode, radius):
        if self.controls is not None:
            self.controls.set_soft_selection(mode, radius)
        self.cv_soft_mode = mode
        self.cv_soft_radius = radius

    def set_control_influence(self, distance, falloff, *, edges=0, by_edges=False, smooth=False):
        if self.face_region is not None:
            return
        self.influence_distance = distance
        self.influence_edges = edges
        self.influence_by_edges = by_edges
        self.influence_smooth = smooth
        self.influence_falloff = tuple(tuple(point) for point in falloff)
        if self.controls is not None:
            self.controls.flush()

    def update_controls(self):
        if self.controls is None:return
        data = self.controls.deform(self.control_base,self.control_ids)
        self.source.write(data,preserve_selection=True)
        self.after,self.ids = data,self.control_ids
        self.changed = self.result_changed(data,self.count)
        self.message('Control curves missing. Undo their deletion or remove and recreate controls.'
                     if self.controls.paths() is None else '')

    def rebuild_controls(self,number):
        if not isinstance(number,int) or not 3<=number<=32:
            raise ValueError('Control point count must be between 3 and 32.')
        if self.controls is None:
            self.create_controls(number)
            return
        if number==self.controls.number:return
        self.update_controls()
        old = self.controls
        # Keep the unaligned profile separately when refitting a smoothed cage.
        # Turning alignment off must restore its detail, including prior CV edits.
        unaligned = (old.deform(self.control_base,self.control_ids,align=False)
                     if self.align_to_curve else self.after)
        # Build the replacement before deleting the original cage. A failed
        # fit leaves the current guides and their live binding available.
        from recurve_controls import ControlCurves, FaceControlCurves
        control_type = FaceControlCurves if self.face_region is not None else ControlCurves
        replacement = control_type(self,number)
        replacement.selection_mode=old.selection_mode
        replacement.component_mask=old.component_mask
        replacement.hilite_names=old.hilite_names
        old.close()
        self.controls = replacement
        self.control_groups.add(replacement.group)
        self.control_base,self.control_ids = unaligned,self.ids
        self.profile_source,self.profile_chains = unaligned,self.ids
        replacement.select()
        replacement.timer.start()
        if self.cv_soft_mode:
            replacement.set_soft_selection(self.cv_soft_mode, self.cv_soft_radius)
        if self.align_to_curve:self.update_controls()
        self.changed = self.result_changed(self.after,self.count)
        self.message()

    def remove_controls(self):
        if self.controls is None:return
        controls,self.controls = self.controls,None
        controls.close()
        self.cleanup_control_groups()
        self.after,self.ids = self.control_base,self.control_ids
        self.source.write(self.after)
        self.control_base,self.control_ids = None,None
        self.changed = self.result_changed(self.after,self.count)
        om.MGlobal.setActiveSelectionList(self.mesh_selection())
        self.message()

    def mesh_selection(self):
        if self.face_region is not None:
            component_fn = om.MFnSingleIndexedComponent()
            component = component_fn.create(om.MFn.kMeshPolygonComponent)
            component_fn.addElements(self.face_region.faces)
            path_selection = om.MSelectionList();path_selection.add(self.path)
            selection = om.MSelectionList();selection.add((path_selection.getDagPath(0),component))
            return selection
        return chain_selection(self.path,self.after,self.ids)

    def cleanup_control_groups(self):
        if not self.control_groups:return
        from recurve_controls import node_path
        groups=[name for uuid in self.control_groups for name in [node_path(uuid)] if name]
        if groups:cmds.delete(groups)
        self.control_groups.clear()

    def save_curves(self):
        """Commit a scene-owned live binding and release the temporary editor."""
        global SESSION,PENDING
        if self.controls is None:raise ValueError('Create control curves before saving them.')
        self.update_controls()
        from recurve_saved_curves import SavedBinding
        binding=SavedBinding(self)
        controls=self.controls
        selected=om.MGlobal.getActiveSelectionList()
        PENDING=(self.path,self.before,self.after,self.selection,selected,self.source,binding)
        try:cmds.recurveApply()
        finally:PENDING=None
        # Do this only after the undoable history binding has succeeded. A
        # failed Save leaves the current preview and its editable CVs intact.
        controls.close(keep=True);self.controls=None
        self.control_groups.discard(controls.group);self.cleanup_control_groups()
        self.label.deleteLater();SESSION=None
        if MENU is not None:
            MENU.control_request_generation+=1
            MENU.sync_controls();MENU.hide()
        return om.MFnDependencyNode(binding.node).name()

    def finish(self,commit):
        global SESSION,PENDING
        if MENU is not None:MENU.control_request_generation += 1
        if self.controls is not None:
            if commit:self.update_controls()
            controls,self.controls = self.controls,None
            controls.close()
        self.cleanup_control_groups()
        if commit and self.changed:
            new_selection = self.mesh_selection()
            PENDING = (self.path,self.before,self.after,self.selection,new_selection,self.source)
            try:
                cmds.recurveApply()
            except Exception:
                self.source.undo()
                om.MGlobal.setActiveSelectionList(self.selection)
                raise
            finally:
                PENDING = None
        else:
            if self._source is not None:
                self._source.undo()
            om.MGlobal.setActiveSelectionList(self.selection)
        self.label.deleteLater()
        SESSION = None
        if MENU is not None:
            MENU.feedback.show_notice('Applied' if commit else 'Cancelled')
            MENU.sync_controls()


def chain_selection(path,data,ids):
    result = om.MFnMesh(data)
    chains = ids if ids and isinstance(ids[0],(list,tuple)) else [ids]
    wanted = {}
    for chain in chains:
        for a, b in zip(chain, chain[1:]):
            wanted.setdefault(a, set()).add(b)
    vertices = om.MItMeshVertex(data)
    selected = set()
    for vertex, neighbors in wanted.items():
        vertices.setIndex(vertex)
        for edge in vertices.getConnectedEdges():
            a, b = result.getEdgeVertices(edge)
            if (b if a == vertex else a) in neighbors:
                selected.add(edge)
    component_fn = om.MFnSingleIndexedComponent()
    component = component_fn.create(om.MFn.kMeshEdgeComponent)
    component_fn.addElements(sorted(selected))
    path_selection = om.MSelectionList(); path_selection.add(path)
    selection = om.MSelectionList()
    selection.add((path_selection.getDagPath(0),component))
    return selection


def start(view=None,allow_faces=False):
    global SESSION
    if SESSION:
        SESSION.finish(True)
        return
    try:
        selection = om.MGlobal.getActiveSelectionList()
        if selection.length()!=1:
            raise ValueError('Select edge chains or polygon faces on one mesh.')
        path,component = selection.getComponent(0)
        faces = component.apiType()==om.MFn.kMeshPolygonComponent and allow_faces
        if not faces and component.apiType()!=om.MFn.kMeshEdgeComponent:
            raise ValueError('Select edge chains, or use Control Curve with polygon faces.')
        fn = om.MFnMesh(path)
        name = path.fullPathName()
        if fn.isInstanced() or cmds.referenceQuery(name,isNodeReferenced=True):
            raise ValueError('Instances and referenced meshes are not supported.')
        # Edit the evaluated snapshot, independent of the upstream node types.
        # MeshSource retains the disconnected input for Cancel and native Undo.
        if mesh_plug(name,'inMesh').isLocked:
            raise ValueError('Mesh input is locked.')
        elements = list(om.MFnSingleIndexedComponent(component).getElements())
        face_region = None
        if faces:
            from recurve_faces import FaceRegion
            face_region = FaceRegion(fn,elements)
            chain = face_region.rows
        else:
            chain = selected_chains(fn,elements)
        if view is None:
            panel = cmds.getPanel(withFocus=True)
            if not panel or cmds.getPanel(typeOf=panel)!='modelPanel':
                raise ValueError('Place the pointer over a viewport and use Shift+Wheel.')
            view = viewport_widget(panel)
        SESSION = Session(name,chain,view,face_region=face_region)
        return SESSION
    except Exception as exc:
        record_error('start',exc)
        cmds.warning('Recurve: '+str(exc))


def viewport_widget(panel):
    """Use the actual drawing widget, rather than the outer modelPanel control."""
    view = omui.M3dView()
    omui.M3dView.getM3dViewFromModelPanel(panel,view)
    return wrapInstance(int(view.widget()),QtWidgets.QWidget)


def event_viewport(obj,event):
    # Native viewport wheel events can arrive on a QWindow or outer panel,
    # without the drawing QWidget in the receiver's parent chain.
    panel = cmds.getPanel(underPointer=True)
    if panel and cmds.getPanel(typeOf=panel)=='modelPanel':
        return viewport_widget(panel)
    for panel in cmds.getPanel(type='modelPanel') or []:
        try:
            view = viewport_widget(panel)
            if obj == view or (isinstance(obj,QtWidgets.QWidget) and view.isAncestorOf(obj)):
                return view
        except (RuntimeError,TypeError):
            continue
    return None


class WheelListener(QtCore.QObject):
    guards_preview_undo = True
    wheel_events = 0
    viewport_events = 0
    alt_tap = False
    space_apply = False
    application_active = True

    @staticmethod
    def wheel_action(modifiers):
        """Choose an action from the active tool, not a transient modifier gap."""
        if not modifiers&QtCore.Qt.ShiftModifier or modifiers&(QtCore.Qt.AltModifier|QtCore.Qt.MetaModifier):return None
        if modifiers&QtCore.Qt.ControlModifier:return 'controls'
        return 'segments' if segment_input_allowed() else 'ignore'

    def set_application_active(self,active):
        # Alt+Tab may swallow releases or deliver them after reactivation.
        # Only transient input state belongs to application focus; preview,
        # settings, control curves and queued edits remain in their session.
        self.application_active = bool(active)
        self.alt_tap = False
        self.space_apply = False

    def eventFilter(self,obj,event):
        try:
            typ = event.type()
            if typ in (QtCore.QEvent.ApplicationActivate,QtCore.QEvent.ApplicationDeactivate):
                self.set_application_active(typ==QtCore.QEvent.ApplicationActivate)
                return False
            if not self.application_active:return False
            if INPUT_GUARD is not None and not INPUT_GUARD(obj,event):return False
            if self.space_apply and typ in (QtCore.QEvent.KeyPress,QtCore.QEvent.KeyRelease) and event.key()==QtCore.Qt.Key_Space:
                if typ==QtCore.QEvent.KeyRelease and not event.isAutoRepeat():self.space_apply=False
                event.accept();return True
            if MENU is not None and QtWidgets.QApplication.activePopupWidget() is not None:
                return False
            if event.type()==QtCore.QEvent.Wheel:
                self.wheel_events += 1
                if event.modifiers() & QtCore.Qt.AltModifier:self.alt_tap=False
                action=self.wheel_action(event.modifiers())
                on_panel = MENU is not None and MENU.isVisible() and MENU.owns(obj)
                if action is None:return False
                view = MENU.view if on_panel else event_viewport(obj,event)
                if view is not None:
                    self.viewport_events += 1
                    delta = event.angleDelta().y()
                    if action=='controls':queue_control_wheel(view,delta)
                    elif action=='segments':queue_wheel(view,delta)
                    event.accept()
                    return True
            # Maya also binds plain Z and Shift+Z to scene Undo/Redo.
            # Claim the shortcut before Maya can change topology under a preview.
            if SESSION is not None and SESSION.controls is None and event.type() in (
                    QtCore.QEvent.ShortcutOverride, QtCore.QEvent.KeyPress):
                modifiers = event.modifiers()
                undo_key = (event.key() == QtCore.Qt.Key_Z and modifiers in (
                    QtCore.Qt.NoModifier, QtCore.Qt.ShiftModifier, QtCore.Qt.ControlModifier,
                    QtCore.Qt.ControlModifier | QtCore.Qt.ShiftModifier))
                redo_key = event.key() == QtCore.Qt.Key_Y and modifiers == QtCore.Qt.ControlModifier
                focus = QtWidgets.QApplication.focusWidget()
                typing = any(isinstance(target, (QtWidgets.QLineEdit, QtWidgets.QAbstractSpinBox))
                             for target in (obj, focus))
                if (undo_key or redo_key) and not typing:
                    if event.type() == QtCore.QEvent.KeyPress and not event.isAutoRepeat():
                        if MENU is not None and hasattr(MENU, 'undo_shortcut'):
                            MENU.undo_shortcut(obj, event)
                        elif undo_key and not modifiers & QtCore.Qt.ShiftModifier:
                            queue_idle(lambda: handle_finish(False))
                    event.accept()
                    return True
            if MENU is not None and MENU.isVisible():
                typ=event.type()
                if typ==QtCore.QEvent.KeyPress and hasattr(MENU,'undo_shortcut') and MENU.undo_shortcut(obj,event):
                    event.accept();return True
                if typ==QtCore.QEvent.KeyPress and event.key() in (QtCore.Qt.Key_Return,QtCore.Qt.Key_Enter):
                    focus=QtWidgets.QApplication.focusWidget()
                    if any(MENU.owns(target) and isinstance(target,(QtWidgets.QLineEdit,QtWidgets.QAbstractSpinBox)) for target in (obj,focus)):
                        if any(target is MENU.control_points or target is MENU.control_points.lineEdit() for target in (obj,focus)):
                            MENU.control_points.interpretText()
                            MENU.queue_control_points(MENU.control_points.value())
                        return False
                if typ==QtCore.QEvent.KeyPress and not event.isAutoRepeat():
                    if event.key()==QtCore.Qt.Key_Alt:
                        self.alt_tap=not bool(event.modifiers() & (QtCore.Qt.ControlModifier|QtCore.Qt.ShiftModifier))
                    else:self.alt_tap=False
                if typ in (QtCore.QEvent.MouseButtonPress,QtCore.QEvent.MouseButtonDblClick) or (typ==QtCore.QEvent.MouseMove and event.buttons()!=QtCore.Qt.NoButton):
                    self.alt_tap=False
                if typ==QtCore.QEvent.KeyRelease and event.key()==QtCore.Qt.Key_Alt and not event.isAutoRepeat():
                    tapped,self.alt_tap=self.alt_tap,False
                    if tapped:
                        queue_idle(MENU.toggle_mode);event.accept();return True
            if SESSION or IDLE_TASKS:
                typ = event.type()
                if typ == QtCore.QEvent.KeyPress:
                    if event.key()==QtCore.Qt.Key_Space and not event.modifiers() and not event.isAutoRepeat() and MENU is not None and MENU.isVisible():
                        focus=QtWidgets.QApplication.focusWidget()
                        editors=(QtWidgets.QLineEdit,QtWidgets.QAbstractSpinBox,QtWidgets.QComboBox,QtWidgets.QAbstractButton)
                        on_view=any(target is MENU.view or (isinstance(target,QtWidgets.QWidget) and MENU.view.isAncestorOf(target)) for target in (obj,focus))
                        if not isinstance(focus,editors) and not isinstance(obj,editors) and (on_view or MENU.owns(obj)):
                            self.space_apply=True
                            queue_idle(lambda: handle_finish(True));event.accept();return True
                    if event.key() in (QtCore.Qt.Key_Escape,QtCore.Qt.Key_Return,QtCore.Qt.Key_Enter):
                        commit = event.key()!=QtCore.Qt.Key_Escape
                        queue_idle(lambda: handle_finish(commit))
                        event.accept()
                        return True
                    if event.key()==QtCore.Qt.Key_Z and event.modifiers() & QtCore.Qt.ControlModifier:
                        focus=QtWidgets.QApplication.focusWidget()
                        if MENU is not None and any(MENU.owns(target) and isinstance(target,(QtWidgets.QLineEdit,QtWidgets.QAbstractSpinBox)) for target in (obj,focus)):return False
                        return not (SESSION is not None and SESSION.controls is not None)
                if typ==QtCore.QEvent.KeyRelease and event.key()==QtCore.Qt.Key_Shift and not event.isAutoRepeat() and MENU_HOST is None and not (MENU and MENU.isVisible()):
                    queue_idle(lambda: handle_finish(True))
        except Exception as exc:
            queue_idle(lambda error=exc: record_error('mouse_input',error))
            if SESSION:
                queue_idle(lambda: handle_finish(False))
            cmds.warning('Recurve mouse input: '+str(exc))
        return False


def segment_input_allowed():
    return not (MENU is not None and getattr(MENU,'section',None)=='controls')


def handle_wheel(view,delta):
    if not segment_input_allowed():return
    session = SESSION or start(view)
    if session:
        session.adjust(delta)


def queue_wheel(view,delta):
    global PENDING_WHEEL_DELTA,PENDING_WHEEL_VIEW
    if not segment_input_allowed():return
    first = PENDING_WHEEL_VIEW is None
    PENDING_WHEEL_DELTA += delta
    PENDING_WHEEL_VIEW = view
    if first:
        queue_idle(dispatch_wheel)


def dispatch_wheel():
    global PENDING_WHEEL_DELTA,PENDING_WHEEL_VIEW
    view,delta = PENDING_WHEEL_VIEW,PENDING_WHEEL_DELTA
    PENDING_WHEEL_VIEW,PENDING_WHEEL_DELTA = None,0
    if view is not None and delta:
        handle_wheel(view,delta)


def queue_control_wheel(view,delta):
    global PENDING_CONTROL_DELTA,PENDING_CONTROL_VIEW
    first=PENDING_CONTROL_VIEW is None
    PENDING_CONTROL_DELTA+=delta;PENDING_CONTROL_VIEW=view
    if first:queue_idle(dispatch_control_wheel)


def dispatch_control_wheel():
    global PENDING_CONTROL_DELTA,PENDING_CONTROL_VIEW
    view,delta=PENDING_CONTROL_VIEW,PENDING_CONTROL_DELTA
    PENDING_CONTROL_VIEW,PENDING_CONTROL_DELTA=None,0
    if view is not None and delta:
        menu=MENU if MENU is not None and MENU.isVisible() else show_menu(view)
        if menu is not None:menu.adjust_control_points(delta,build=True)


def handle_finish(commit):
    if SESSION:
        SESSION.finish(commit)


class RecurveMenu(QtWidgets.QDialog):
    def __init__(self,view,parent=None):
        super().__init__(parent,QtCore.Qt.Tool)
        self.view = view
        self.control_request_generation = 0
        from recurve_panel import build_panel
        build_panel(self,CONNECT_NEW,VERSION,PANEL_STYLE,PANEL_LAYOUT)
        self.closed = False
        self.control_wheel = 0
        self.apply.clicked.connect(lambda: queue_idle(lambda: handle_finish(True)))
        self.cancel.clicked.connect(lambda: queue_idle(lambda: handle_finish(False)))
        self.count.valueChanged.connect(lambda value: queue_idle(lambda: self.change_count(value)))
        self.connect.toggled.connect(self.mode_toggled)
        self.mode.currentIndexChanged.connect(lambda index: (self.connect if index else self.ngons).setChecked(True))
        self.style_picker.currentTextChanged.connect(self.set_panel_style)
        self.layout_picker.currentTextChanged.connect(self.set_panel_layout)
        self.control_points.valueChanged.connect(self.queue_control_points)
        self.select_curve_cvs_action.triggered.connect(lambda: queue_idle(self.create_control_curves))
        self.reset_curves.clicked.connect(lambda: queue_idle(lambda: self.control_action('reset')))
        self.remove_curves.clicked.connect(lambda: queue_idle(lambda: self.control_action('remove')))
        if SESSION:
            self.sync(SESSION.count)
        else:
            self.refresh_selection()

    def refresh_selection(self):
        try:
            selection = om.MGlobal.getActiveSelectionList()
            path,component = selection.getComponent(0)
            if component.apiType()==om.MFn.kMeshPolygonComponent:
                self.closed = False
                self.feedback.setText('')
                return
            chain = selected_chains(om.MFnMesh(path),list(om.MFnSingleIndexedComponent(component).getElements()))[0]
            self.closed = chain[0]==chain[-1]
            self.sync(len(chain)-1)
        except Exception:
            self.feedback.setText('Select edges on one mesh first.')

    def activate_preview(self):
        if MENU is self and self.isVisible():
            self.begin_preview()

    def sync(self,count,error=''):
        previous = self.count.blockSignals(True)
        if SESSION:self.closed=SESSION.chain[0]==SESSION.chain[-1]
        self.count.setMinimum(3 if self.closed else 1)
        self.count.setValue(count)
        self.count.blockSignals(previous)
        self.feedback.setText(error)
        self.sync_controls()

    def sync_controls(self):
        from recurve_panel import fit_panel
        active = SESSION is not None and SESSION.controls is not None
        self.controls_active = active
        self.select_curve_cvs_action.setEnabled(active)
        self.control_points.setEnabled(True)
        self.reset_curves.setEnabled(active);self.remove_curves.setEnabled(active)
        self.reset_curves.setVisible(active);self.remove_curves.setVisible(active)
        self.curve_actions.setVisible(active)
        self.live_badge.setText('LIVE' if SESSION is not None else 'READY')
        fit_panel(self)

    def owns(self,obj):
        return isinstance(obj,QtWidgets.QWidget) and (obj is self or self.isAncestorOf(obj))

    def set_panel_style(self,name):
        global PANEL_STYLE
        from recurve_panel import set_style
        PANEL_STYLE=name
        set_style(self,name)

    def set_panel_layout(self,name):
        global PANEL_LAYOUT
        PANEL_LAYOUT=name

    def mode_toggled(self,value):
        blocked=self.mode.blockSignals(True)
        self.mode.setCurrentIndex(1 if value else 0)
        self.mode.blockSignals(blocked)
        queue_idle(lambda: self.change_mode(value))

    def toggle_mode(self):
        self.mode.setCurrentIndex(1-self.mode.currentIndex())

    def adjust_control_points(self,delta,build=False):
        self.control_wheel+=delta
        steps=int(self.control_wheel/120)
        self.control_wheel-=steps*120
        if not steps:return
        self.control_points.setValue(max(3,min(32,self.control_points.value()+steps)))
        if build and (SESSION is None or SESSION.controls is None):self.create_control_curves()

    def queue_control_points(self,value):
        generation = self.control_request_generation
        queue_idle(lambda: self.change_control_points(value,generation))

    def change_control_points(self,value,generation=None):
        if generation is not None and generation!=self.control_request_generation:return
        if value!=self.control_points.value() or not self.isVisible():return
        try:
            if SESSION is None:self.begin_preview(controls=True)
            if SESSION is not None:SESSION.rebuild_controls(value)
        except Exception as exc:
            record_error('control_point_count',exc)
            self.feedback.setText(str(exc))
            if SESSION is not None and SESSION.controls is not None:
                blocked=self.control_points.blockSignals(True)
                self.control_points.setValue(SESSION.controls.number)
                self.control_points.blockSignals(blocked)

    def create_control_curves(self):
        try:
            if SESSION is None:self.begin_preview(controls=True)
            if SESSION:SESSION.create_controls(self.control_points.value())
        except Exception as exc:
            record_error('create_controls',exc)
            self.feedback.setText(str(exc))

    def control_action(self,action):
        if SESSION is None or SESSION.controls is None:return
        try:
            if action=='reset':SESSION.controls.reset()
            else:SESSION.remove_controls()
        except Exception as exc:
            record_error('control_'+action,exc)
            self.feedback.setText(str(exc))

    def begin_preview(self,controls=False):
        global CONNECT_NEW
        CONNECT_NEW = self.connect.isChecked()
        if SESSION is None:
            start(self.view,allow_faces=controls)
        if SESSION:
            self.sync(SESSION.count)
        else:
            self.feedback.setText('Could not start. See Script Editor for details.')

    def change_count(self,value):
        if SESSION is None and self.isVisible():
            self.begin_preview()
        if SESSION:
            SESSION.adjust((value-SESSION.count)*120)

    def change_mode(self,value):
        global CONNECT_NEW
        CONNECT_NEW = value
        if SESSION is None and self.isVisible():
            self.begin_preview()
        if SESSION:
            try:
                SESSION.set_mode(value)
            except Exception as exc:
                record_error('change_mode',exc)
                self.feedback.setText(str(exc))

    def closeEvent(self,event):
        queue_idle(lambda: handle_finish(False))
        event.accept()


def show_menu(view=None):
    global MENU
    if MENU_HOST is not None:
        embedded = MENU_HOST(view)
        if embedded is not None:return embedded
    if view is None:
        panel = cmds.getPanel(underPointer=True) or cmds.getPanel(withFocus=True)
        if panel and cmds.getPanel(typeOf=panel)=='modelPanel':
            view = viewport_widget(panel)
        else:
            panels = cmds.getPanel(type='modelPanel') or []
            if not panels:
                cmds.warning('Recurve: no viewport is available.'); return
            view = viewport_widget(panels[0])
    if MENU is not None:
        MENU.view = view
        if SESSION is None: MENU.refresh_selection()
        MENU.placement.place()
        MENU.show(); MENU.raise_()
        queue_idle(MENU.activate_preview)
        return MENU
    pointer = omui.MQtUtil.mainWindow()
    parent = wrapInstance(int(pointer),QtWidgets.QWidget) if pointer else None
    MENU = RecurveMenu(view,parent)
    MENU.placement.place()
    MENU.show(); MENU.raise_()
    queue_idle(MENU.activate_preview)
    return MENU


def install(quiet=False,bind_hotkey=True):
    """Install a persistent Shift+Wheel listener, including after plug-in autoload."""
    global LISTENER
    app = QtWidgets.QApplication.instance()
    if app is None:
        if cmds.about(batch=True):
            return False
        raise RuntimeError('Recurve: Qt application is unavailable. Run the installer inside Maya Script Editor.')
    if LISTENER is not None:
        app.removeEventFilter(LISTENER)
        LISTENER.deleteLater()
    LISTENER = WheelListener(app)
    app.installEventFilter(LISTENER)
    if not SCENE_CALLBACKS:
        SCENE_CALLBACKS.extend([om.MSceneMessage.addCallback(event,before_scene_change)
                                for event in (om.MSceneMessage.kBeforeNew,om.MSceneMessage.kBeforeOpen)])
        SCENE_CALLBACKS.append(om.MSceneMessage.addCallback(
            om.MSceneMessage.kMayaExiting, lambda *_: shutdown()))
    if bind_hotkey and not cmds.about(batch=True):
        current = cmds.hotkeySet(query=True,current=True)
        if not cmds.hotkeySet('Recurve',exists=True):
            cmds.hotkeySet('Recurve',source=current)
        cmds.hotkeySet('Recurve',edit=True,current=True)
        folder = os.path.dirname(os.path.abspath(__file__))
        plugin_path = os.path.join(folder,'recurve.py')
        body = ('import sys\nfolder = '+repr(folder)+'\n'
                'if folder in sys.path: sys.path.remove(folder)\n'
                'sys.path.insert(0, folder)\n'
                'import maya.cmds as cmds\n'
                'if not cmds.pluginInfo("recurve", query=True, loaded=True): cmds.loadPlugin('+repr(plugin_path)+')\n'
                'import recurve\nrecurve.install(quiet=True, bind_hotkey=False)\nrecurve.show_menu()')
        if cmds.runTimeCommand('RecurveMenu',exists=True):
            cmds.runTimeCommand('RecurveMenu',edit=True,command=body,commandLanguage='python')
        else:
            cmds.runTimeCommand('RecurveMenu',command=body,commandLanguage='python',category='User')
        cmds.nameCommand('RecurveMenuName',annotation='Recurve menu',command='RecurveMenu')
        cmds.hotkey(keyShortcut='r',shiftModifier=True,name='RecurveMenuName')
        cmds.savePrefs(hotkeys=True)
    if not quiet:
        print('RECURVE '+VERSION+' / MODEL PACK '+MODEL_PACK_BUILD+' ENABLED: Shift+R menu; live shaded mesh; Triangles / N-gons.')
        cmds.inViewMessage(amg='Recurve: <hl>Shift+R</hl> — menu / live preview.',pos='topCenter',fade=True)
    return True


def status():
    print('Recurve module: '+str(globals().get('__file__','unknown')))
    print('Listener: '+('ENABLED' if LISTENER is not None else 'DISABLED'))
    if LISTENER is not None:
        print('Wheel events: %d; viewport wheel events: %d' % (LISTENER.wheel_events,LISTENER.viewport_events))


def shutdown():
    global LISTENER,MENU,PENDING_WHEEL_VIEW,PENDING_WHEEL_DELTA,PENDING_CONTROL_VIEW,PENDING_CONTROL_DELTA
    IDLE_TASKS.clear()
    PENDING_WHEEL_VIEW,PENDING_WHEEL_DELTA = None,0
    PENDING_CONTROL_VIEW,PENDING_CONTROL_DELTA = None,0
    if MENU is not None:
        MENU.hide(); MENU.deleteLater(); MENU=None
    if SESSION:
        SESSION.finish(False)
    if LISTENER is not None:
        QtWidgets.QApplication.instance().removeEventFilter(LISTENER)
        LISTENER.deleteLater()
        LISTENER = None
    if SCENE_CALLBACKS:
        om.MMessage.removeCallbacks(SCENE_CALLBACKS)
        SCENE_CALLBACKS.clear()


def before_scene_change(*args):
    global PENDING_WHEEL_VIEW,PENDING_WHEEL_DELTA,PENDING_CONTROL_VIEW,PENDING_CONTROL_DELTA
    IDLE_TASKS.clear()
    PENDING_WHEEL_VIEW,PENDING_WHEEL_DELTA = None,0
    PENDING_CONTROL_VIEW,PENDING_CONTROL_DELTA = None,0
    if SESSION:
        SESSION.finish(False)
    if MENU:
        MENU.hide()


def initializePlugin(obj):
    import os
    plugin = om.MFnPlugin(obj,'Recurve',VERSION,'Any')
    folder = plugin.loadPath()
    if folder not in sys.path:
        sys.path.insert(0,folder)
    import recurve
    plugin.registerCommand('recurveApply',lambda: recurve.Apply())
    if not cmds.about(batch=True):
        cmds.evalDeferred('import recurve; recurve.install(quiet=True)')


def uninitializePlugin(obj):
    import recurve
    recurve.shutdown()
    om.MFnPlugin(obj).deregisterCommand('recurveApply')

