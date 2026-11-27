"""Cached geometric edge angles for live Hard Edges selection."""
import bisect
import math

from maya import cmds
from maya.api import OpenMaya as om
from slide_for_maya.core import _mesh_fn


def selected_meshes():
    """Resolve objects, shapes and components without expanding every edge."""
    shapes=[]
    for node in cmds.ls(selection=True,objectsOnly=True,long=True) or []:
        candidates=([node] if cmds.nodeType(node)=='mesh' else
                    cmds.listRelatives(node,shapes=True,noIntermediate=True,type='mesh',fullPath=True) or [])
        for shape in candidates:
            if shape not in shapes and not cmds.getAttr(shape+'.intermediateObject'):shapes.append(shape)
    if not shapes:raise ValueError('Выделите меш или его компоненты для Hard Edges.')
    return shapes


def selected_face_regions():
    selection = om.MGlobal.getActiveSelectionList()
    regions = {}
    for index in range(selection.length()):
        try:
            path, component = selection.getComponent(index)
        except RuntimeError:
            continue
        if component.isNull() or component.apiType() != om.MFn.kMeshPolygonComponent:
            continue
        if path.node().hasFn(om.MFn.kTransform):
            path.extendToShape()
        faces = om.MFnSingleIndexedComponent(component)
        ids = range(faces.getCompleteData()) if faces.isComplete else faces.getElements()
        regions.setdefault(path.fullPathName(), set()).update(ids)
    return regions


def _edge_angles(path, region=None):
    fn=om.MFnMesh(path)
    normals={}
    edges=om.MItMeshEdge(path);values=[]
    if region is not None:
        polygons = om.MItMeshPolygon(path)
        candidates = set()
        for face in region:
            if face < fn.numPolygons:
                polygons.setIndex(face)
                candidates.update(polygons.getEdges())
    else:
        candidates = range(fn.numEdges)
    for edge in candidates:
        edges.setIndex(edge)
        faces=edges.getConnectedFaces()
        # An angle requires two adjacent faces; open/nonmanifold edges have
        # no single dihedral angle.
        if len(faces)==2:
            for face in faces:
                if face not in normals:
                    normals[face] = fn.getPolygonNormal(face, om.MSpace.kWorld)
            a,b=(normals[f] for f in faces)
            if a.length()>1e-12 and b.length()>1e-12:
                cosine=max(-1.,min(1.,a.normal()*b.normal()))
                values.append((math.degrees(math.acos(cosine)),edge))
    values.sort()
    return tuple(v[0] for v in values),tuple(v[1] for v in values)


def _components(mask):
    result=[]
    for mesh,edges in mask.items():
        ordered=sorted(edges)
        if not ordered:continue
        first=last=ordered[0]
        for edge in ordered[1:]+[None]:
            if edge is not None and edge==last+1:last=edge;continue
            result.append(mesh+f'.e[{first}]' if first==last else mesh+f'.e[{first}:{last}]')
            first=last=edge
    return result


def _selected_mask():
    selection=om.MGlobal.getActiveSelectionList();mask={}
    for index in range(selection.length()):
        try:path,component=selection.getComponent(index)
        except RuntimeError:return None
        if component.isNull() or component.apiType()!=om.MFn.kMeshEdgeComponent:return None
        if path.node().hasFn(om.MFn.kTransform):path.extendToShape()
        mask.setdefault(path.fullPathName(),set()).update(om.MFnSingleIndexedComponent(component).getElements())
    return mask


class Selection:
    """Keep the activation targets even if a threshold deselects all edges."""
    def __init__(self,meshes,face_regions=None):
        self.paths=[_mesh_fn(mesh).dagPath() for mesh in meshes]
        self.regions = [(path, (face_regions or {}).get(path.fullPathName())) for path in self.paths]
        self.dirty=True;self.tables=[];self.callbacks=[]
        try:
            seen=set()
            for path in self.paths:
                ancestor=om.MDagPath(path)
                while ancestor.length():
                    node=ancestor.node();key=om.MObjectHandle(node).hashCode()
                    if key not in seen:
                        seen.add(key)
                        self.callbacks.append(om.MNodeMessage.addNodeDirtyCallback(node,self.invalidate))
                    ancestor.pop()
            for event in ('Undo','Redo'):
                self.callbacks.append(om.MEventMessage.addEventCallback(event,self.invalidate))
        except Exception:
            self.close();raise

    def invalidate(self,*_):self.dirty=True

    def close(self):
        for callback in self.callbacks:om.MMessage.removeCallback(callback)
        self.callbacks=[];self.tables=[]

    def _live_paths(self):
        return [path for path in self.paths if path.isValid() and om.MObjectHandle(path.node()).isValid()]

    def mask(self,angle):
        paths=self._live_paths()
        if not paths:raise ValueError('Исходные меши удалены. Выделите меш и снова включите Hard Edges.')
        if self.dirty or len(paths)!=len(self.tables):
            self.tables=[(path,*_edge_angles(path, region))
                         for path, region in self.regions if path in paths]
            self.dirty=False
        result={}
        for path,angles,edges in self.tables:
            chosen=set(edges[bisect.bisect_left(angles,angle-1e-7):])
            if chosen:result[path.fullPathName()]=chosen
        return result

    def apply(self,angle,*,configure=False):
        mask=self.mask(angle)
        # Restore the edge mask if the user changed component type while
        # keeping the panel open. Standalone Maya has no UI mask (None).
        edge_mode=cmds.selectType(query=True,polymeshEdge=True)
        if edge_mode is not None and (not edge_mode or not cmds.selectMode(query=True,component=True)):
            configure=True
        if configure:
            cmds.selectMode(component=True)
            cmds.selectType(allComponents=False,polymeshEdge=True)
            parents=[]
            for path in self._live_paths():
                parent=om.MDagPath(path);parent.pop()
                if parent.fullPathName() not in parents:parents.append(parent.fullPathName())
            cmds.hilite(parents,replace=True)
        changed=_selected_mask()!=mask
        if changed:
            components=_components(mask)
            if components:cmds.select(components,replace=True)
            else:cmds.select(clear=True)
        if (changed or configure) and not cmds.about(batch=True):cmds.refresh(currentView=True)
        return sum(map(len,mask.values()))


def harden_selected():
    """Maya Mesh Display / Harden Edge, on the current edge selection only."""
    mask=_selected_mask()
    if not mask:raise ValueError('Выделите рёбра для Harden Edge.')
    edges=_components(mask)
    cmds.undoInfo(openChunk=True,chunkName='Harden Edge')
    try:
        cmds.polySoftEdge(edges,angle=0,constructionHistory=True)
        # Some Maya modeling commands replace components with the history
        # result. Keep the user's edge selection after applying normals.
        if _selected_mask()!=mask:cmds.select(edges,replace=True)
        if not cmds.about(batch=True):cmds.refresh(currentView=True)
    finally:cmds.undoInfo(closeChunk=True)
    return sum(map(len,mask.values()))
