"""Bounded search results, invalidated by Maya mesh and history changes."""
from collections import OrderedDict
import copy
import hashlib
from array import array
from maya.api import OpenMaya as om
from slide_for_maya.core import _mesh_fn


class Entry:
    def __init__(self, fn, signature):
        self.handle=om.MObjectHandle(fn.object());self.signature=signature
        self.version=0;self.results={};self.bands={};self.prepared={};self.callbacks=[]
        path=om.MDagPath(fn.dagPath())
        try:
            while path.length():
                node=path.node()
                self.callbacks.append(om.MNodeMessage.addNodeDirtyCallback(node,self.invalidate))
                self.callbacks.append(om.MNodeMessage.addAttributeChangedCallback(node,self.attribute_changed))
                self.callbacks.append(om.MNodeMessage.addNameChangedCallback(node,self.invalidate))
                path.pop()
        except Exception:
            self.close()
            raise

    def invalidate(self, *_):
        self.version+=1;self.results.clear();self.bands.clear();self.prepared.clear()
        self.topology_identity=None

    def attribute_changed(self, message, *_):
        # Reads also emit kAttributeEval; they do not change the snapshot.
        changed=(om.MNodeMessage.kAttributeSet|om.MNodeMessage.kAttributeAdded|
                 om.MNodeMessage.kAttributeRemoved|om.MNodeMessage.kConnectionMade|
                 om.MNodeMessage.kConnectionBroken|om.MNodeMessage.kAttributeArrayAdded|
                 om.MNodeMessage.kAttributeArrayRemoved)
        if message & changed:self.invalidate()

    def close(self):
        for callback in self.callbacks:
            try:om.MMessage.removeCallback(callback)
            except RuntimeError:pass
        self.callbacks.clear();self.invalidate()

    def remember(self, key, result, prepared=(), *, reusable=()):
        self.results[key]=result
        # Retain finished groups and reconstruction only, not growing-strip
        # caches or complete point buffers. Bound large object searches too.
        if key[0] is not None:
            for group in result.groups:
                for face in set(group.faces)&set(reusable):
                    index=key[1:],face
                    previous=self.bands.get(index,group)
                    self.bands[index]=group if previous is not None and previous.faces==group.faces else None
        self.prepared.update(prepared)
        faces=sum(len(g.faces) for result in self.results.values() for g in result.groups)
        vertices=sum(sum(map(len,value[0][1])) for value in self.prepared.values())
        if (len(self.results)>64 or len(self.bands)>100000 or len(self.prepared)>256
                or faces+vertices>250000):
            self.invalidate()

    def seeded(self, key):
        seeds=key[0]
        if not seeds:return None
        from .bevel_detection import Detection,replace
        # The button selects whole bands after the initial single-face query.
        # Recognize that complete mask before consulting ambiguous shared cells.
        for previous,result in reversed(tuple(self.results.items())):
            if previous[1:]!=key[1:]:continue
            chosen=[g for g in result.groups if set(g.faces)<=seeds
                    and (previous[0] is not None or g.ready)]
            if chosen and {f for g in chosen for f in g.faces}==seeds:
                return Detection(tuple(replace(g,source='seed') if previous[0] is None else g for g in chosen))
        groups=[];covered=set()
        for seed in sorted(seeds):
            if seed in covered:continue
            group=self.bands.get((key[1:],seed))
            # Shared intersections are excluded during local classification.
            if group is None:return None
            groups.append(group);covered.update(group.faces)
        return Detection(tuple(groups))


class SearchCache:
    def __init__(self):self.entries=OrderedDict();self.callbacks=[]

    def invalidate(self, *_):
        for entry in self.entries.values():entry.invalidate()

    def clear(self, *_):
        for entry in self.entries.values():entry.close()
        self.entries.clear()

    def shutdown(self):
        self.clear()
        for callback in self.callbacks:
            try:om.MMessage.removeCallback(callback)
            except RuntimeError:pass
        self.callbacks.clear()

    def get(self, mesh):
        fn=_mesh_fn(mesh);path=fn.dagPath()
        identity=om.MObjectHandle(fn.object()).hashCode(),path.fullPathName()
        signature=fn.numVertices,fn.numEdges,fn.numPolygons,tuple(path.inclusiveMatrix())
        if not self.callbacks:
            self.callbacks=[om.MEventMessage.addEventCallback(event,self.invalidate)
                            for event in ('Undo','Redo')]
            self.callbacks.extend(om.MSceneMessage.addCallback(event,self.clear)
                                  for event in (om.MSceneMessage.kBeforeNew,om.MSceneMessage.kBeforeOpen))
        for key,entry in list(self.entries.items()):
            if not entry.handle.isValid() or not entry.handle.isAlive():
                entry.close();del self.entries[key]
        entry=self.entries.get(identity)
        if entry is None:
            entry=self.entries[identity]=Entry(fn,signature)
        elif entry.signature!=signature:
            entry.invalidate();entry.signature=signature
        self.entries.move_to_end(identity)
        while len(self.entries)>4:self.entries.popitem(last=False)[1].close()
        return entry


if 'CACHE' in globals():CACHE.shutdown()
CACHE=SearchCache()


def prepare(mesh, faces, *, details=None):
    """Reuse a checked reconstruction immediately after search."""
    entry=CACHE.get(mesh);key=frozenset(faces)
    if key in entry.prepared:
        result,scratch=entry.prepared[key]
        if details is not None:details.update(copy.deepcopy(scratch))
        return ([om.MPoint(p) for p in result[0]],[list(row) for row in result[1]],list(result[2]))
    from . import unbevel_faces
    return unbevel_faces.prepare(mesh,key,details=details)


def _topology_fingerprint(fn):
    counts,vertices=fn.getVertices();digest=hashlib.sha256()
    digest.update(array('i',counts).tobytes());digest.update(array('i',vertices).tobytes())
    return (om.MFnDependencyNode(fn.object()).uuid().asString(),fn.numVertices,
            fn.numEdges,fn.numPolygons,len(vertices),digest.digest())


def topology_identity(mesh):
    """Compact exact-connectivity signature reused until the mesh is dirty."""
    entry=CACHE.get(mesh)
    identity=getattr(entry,'topology_identity',None)
    if identity is None:
        identity=entry.topology_identity=_topology_fingerprint(_mesh_fn(mesh))
    return identity
