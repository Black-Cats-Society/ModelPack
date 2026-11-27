"""Data-only bevel junction topology; API 1 supports named UV/color creation."""
import json,sys
from pathlib import Path
root=str(Path(globals().get('__file__',sys._getframe().f_code.co_filename)).resolve().parent)
if root not in sys.path:sys.path.insert(0,root)
from maya import OpenMaya as old,OpenMayaMPx as mpx
from maya.api import OpenMaya as om
import model_pack_legacy_mesh as backend
backend.JUNCTION_NODE_CLASSES=getattr(backend,'JUNCTION_NODE_CLASSES',set())
from model_pack_bevel.junctions import build, join_sharp_rows

NAME='modelPackBevelJunctions';ID=old.MTypeId(0x0013B2F7)


def _polygons(mesh):
    sizes, ids = mesh.getVertices()
    result, offset = [], 0
    for size in sizes:
        result.append(ids[offset:offset + size])
        offset += size
    return result


def _write(source,points,faces,owners,records,splits,vertex_remap=None):
    src=backend.Mesh(source);data=backend.Data().create();out=backend.Mesh()
    out.create(points,[len(f) for f in faces],[v for f in faces for v in f],data)
    counts,_=src.getVertices();offsets=[];offset=0
    for count in counts:offsets.append(offset);offset+=count
    for name in src.getUVSetNames():
        us,vs=src.getUVs(name);uc,ui=src.getAssignedUVs(name);uv_offsets=[];offset=0
        for count in uc:uv_offsets.append(offset);offset+=count
        cache={};assigned=[];assigned_counts=[]
        for owner,part in zip(owners,records):
            # Native Bevel can report -1 UV IDs for an unmapped end face.
            # API 1 requires that face to use a zero assignment count.
            if uc[owner]!=counts[owner] or any(i<0 or i>=len(us) for i in ui[uv_offsets[owner]:uv_offsets[owner]+uc[owner]]):
                assigned_counts.append(0);continue
            assigned_counts.append(len(part))
            for weights in part:
                ids={}
                for corner,weight in weights.items():
                    index=ui[uv_offsets[owner]+corner];ids[index]=ids.get(index,0)+weight
                if len(ids)==1:index=next(iter(ids))
                else:
                    key=tuple(sorted((i,round(w,12)) for i,w in ids.items()))
                    if key not in cache:
                        cache[key]=len(us);us.append(sum(us[i]*w for i,w in ids.items()));vs.append(sum(vs[i]*w for i,w in ids.items()))
                    index=cache[key]
                assigned.append(index)
        if name not in out.getUVSetNames():out.createUVSet(name)
        if us:
            out.setUVs(us,vs,name)
            out.assignUVs(assigned_counts,assigned,name)
    for name in src.colorSets():
        rep=src.fn.getColorRepresentation(name);clamped=src.fn.isColorClamped(name)
        palette=old.MColorArray();src.fn.getColors(palette,name)
        values=[(palette[i].r,palette[i].g,palette[i].b,palette[i].a) for i in range(palette.length())]
        original=[]
        for face,count in enumerate(counts):
            for corner in range(count):
                util=old.MScriptUtil();util.createFromInt(0);ptr=util.asIntPtr()
                src.fn.getColorIndex(face,corner,ptr,name);original.append(old.MScriptUtil.getInt(ptr))
        cache={};assigned=[]
        for owner,part in zip(owners,records):
            for weights in part:
                ids={}
                for corner,weight in weights.items():
                    index=original[offsets[owner]+corner];ids[index]=ids.get(index,0)+weight
                if any(i<0 for i in ids):index=-1
                elif len(ids)==1:index=next(iter(ids))
                else:
                    key=tuple(sorted((i,round(w,12)) for i,w in ids.items()))
                    if key not in cache:cache[key]=len(values);values.append(tuple(sum(values[i][c]*w for i,w in ids.items()) for c in range(4)))
                    index=cache[key]
                assigned.append(index)
        out.fn.createColorSetWithNameDataMesh(name,clamped,rep)
        if values:
            array=old.MColorArray()
            for value in values:array.append(old.MColor(*value))
            out.fn.setColors(array,name,rep);out.fn.assignColors(backend.ints(assigned),name)
    flags={frozenset(src.getEdgeVertices(e)):src.isEdgeSmooth(e) for e in range(src.numEdges)}
    if vertex_remap is not None:
        smooth = {}
        for edge, flag in flags.items():
            if edge <= vertex_remap.keys():
                remapped = frozenset(vertex_remap[v] for v in edge)
                smooth[remapped] = smooth.get(remapped, True) and flag
        flags = smooth
    for edge,midpoint in splits.items():
        for v in edge:flags[frozenset((v,midpoint))]=flags.get(frozenset(edge),True)
    locked=src.lockedNormals()
    if locked:
        mask,values,normal_ids=locked;normals=[];normal_faces=[];vertices=[]
        for face,(owner,polygon,part) in enumerate(zip(owners,faces,records)):
            for vertex,weights in zip(polygon,part):
                if len(weights)!=1:continue
                corner=next(iter(weights));n=normal_ids[offsets[owner]+corner]
                if mask[n]:normals.append(values[n]);normal_faces.append(face);vertices.append(vertex)
        if normals:out.setLockedNormals(normals,normal_faces,vertices)
    for edge in range(out.numEdges):out.setEdgeSmoothing(edge,flags.get(frozenset(out.getEdgeVertices(edge)),True))
    out.cleanupEdgeSmoothing()
    src_groups=backend.Data(source);dst=backend.Data(data);dst.copyObjectGroups(source)
    for i in range(src_groups.objectGroupCount):
        group=src_groups.objectGroup(i);component=src_groups.objectGroupComponent(group)
        if component.isNull() or component.apiType()!=old.MFn.kMeshPolygonComponent:continue
        old_comp=backend.Component(component);members=None if old_comp.isComplete else set(old_comp.getElements())
        new=backend.Component();component=new.create(old.MFn.kMeshPolygonComponent)
        new.addElements([f for f,owner in enumerate(owners) if members is None or owner in members]);dst.setObjectGroupComponent(group,component)
    return data


class JunctionNode(mpx.MPxNode):
    def __init__(self):super().__init__();backend.JUNCTION_NODE_CLASSES.add(type(self))
    @staticmethod
    def creator():return mpx.asMPxPtr(JunctionNode())
    @staticmethod
    def initialize():
        cls=JunctionNode;inputs=[];outputs=[]
        for name,short,output in (('sourceMesh','src',False),('flatMesh','flat',False),('roundMesh','rnd',False),('output','out',True),('roundOutput','rout',True)):
            typed=old.MFnTypedAttribute();attr=typed.create(name,short,old.MFnData.kMesh)
            typed.setStorable(False);typed.setHidden(True);typed.setWritable(not output);cls.addAttribute(attr)
            (outputs if output else inputs).append(attr)
        typed=old.MFnTypedAttribute();attr=typed.create('selectedEdges','edges',old.MFnData.kString);typed.setHidden(True);cls.addAttribute(attr);inputs.append(attr)
        numeric=old.MFnNumericAttribute();attr=numeric.create('referenceWidth','ref',old.MFnNumericData.kDouble,.001);numeric.setHidden(True);cls.addAttribute(attr);inputs.append(attr)
        matrix=old.MFnMatrixAttribute();attr=matrix.create('meshMatrix','mat');matrix.setHidden(True);cls.addAttribute(attr);inputs.append(attr)
        typed=old.MFnTypedAttribute();attr=typed.create('pointBlends','blends',old.MFnData.kString)
        typed.setStorable(False);typed.setHidden(True);typed.setWritable(False);cls.addAttribute(attr);outputs.append(attr)
        for source in inputs:
            for target in outputs:cls.attributeAffects(source,target)
    def compute(self,plug,data):
        fn=old.MFnDependencyNode(self.thisMObject())
        if plug not in (fn.attribute('output'),fn.attribute('roundOutput'),fn.attribute('pointBlends')):return old.kUnknownParameter
        source=data.inputValue(fn.attribute('sourceMesh')).asMesh();flat=data.inputValue(fn.attribute('flatMesh')).asMesh();rounded=data.inputValue(fn.attribute('roundMesh')).asMesh()
        if source.isNull() or flat.isNull() or rounded.isNull():return
        src,a,b=backend.Mesh(source),backend.Mesh(flat),backend.Mesh(rounded)
        polygons = _polygons(a)
        selected=json.loads(data.inputValue(fn.attribute('selectedEdges')).asString())
        raw=data.inputValue(fn.attribute('meshMatrix')).asMatrix()
        matrix=om.MMatrix([old.MScriptUtil.getDoubleArrayItem(raw[row],col) for row in range(4) for col in range(4)])
        reference = data.inputValue(fn.attribute('referenceWidth')).asDouble()
        blends = {}
        if fn.hasAttribute('sharpJoin'):
            joined = join_sharp_rows(src.getPoints(), a.getPoints(), b.getPoints(), polygons, _polygons(b),
                                     [src.getEdgeVertices(e) for e in selected], reference, matrix)
            if joined:
                points, faces, owners, records, remap = joined
                flat = _write(flat, points, faces, owners, records, {}, remap)
            rounded = flat
        else:
            result = build(src.getPoints(), a.getPoints(), b.getPoints(), polygons, selected,
                           {e: src.getEdgeVertices(e) for e in selected}, reference, matrix)
            if result:
                first, second, faces, owners, records, splits = result
                original_count = len(a.getPoints())
                for polygon, owner, part in zip(faces, owners, records):
                    for vertex, weights in zip(polygon, part):
                        if vertex >= original_count:
                            blends[vertex] = {polygons[owner][c]: w for c, w in weights.items()}
                flat = _write(flat, first, faces, owners, records, splits)
                rounded = _write(rounded, second, faces, owners, records, splits)
        for name,value in (('output',flat),('roundOutput',rounded)):
            attr=fn.attribute(name);data.outputValue(attr).setMObject(value);data.setClean(old.MPlug(self.thisMObject(),attr))
        attr=fn.attribute('pointBlends');data.outputValue(attr).setString(json.dumps(blends));data.setClean(old.MPlug(self.thisMObject(),attr))


def initializePlugin(plugin):mpx.MFnPlugin(plugin,'Model Pack','1.0','Any').registerNode(NAME,ID,JunctionNode.creator,JunctionNode.initialize)
def uninitializePlugin(plugin):mpx.MFnPlugin(plugin).deregisterNode(ID)
