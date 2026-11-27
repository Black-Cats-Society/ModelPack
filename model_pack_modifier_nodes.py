"""Persistent geometry history for Model Pack modifiers."""
import json,sys
from pathlib import Path
root=str(Path(globals().get('__file__',sys._getframe().f_code.co_filename)).resolve().parent)
if root not in sys.path:sys.path.insert(0,root)
from maya.api import OpenMaya as om


def maya_useNewAPI():pass


def _mesh_attr(cls,name,short,output=False):
    typed=om.MFnTypedAttribute();attr=typed.create(name,short,om.MFnData.kMesh)
    typed.storable=False;typed.hidden=True;typed.writable=not output;typed.readable=True
    cls.addAttribute(attr);setattr(cls,name,attr);return attr


def _numeric(cls,name,short,kind,default):
    numeric=om.MFnNumericAttribute();attr=numeric.create(name,short,kind,default)
    numeric.keyable=True;cls.addAttribute(attr);setattr(cls,name,attr);return attr


def _finish(data,plug,attr,output):
    data.outputValue(attr).setMObject(output);data.setClean(plug)


def _finish_tube_mesh(output,caps):
    """Keep each closed end as one polygon; weld UVs and shade its rim."""
    fn=om.MFnMesh(output);cap_faces=set(caps)
    counts,vertices=fn.getVertices();uvcounts,uvids=fn.getAssignedUVs()
    u,v=fn.getUVs();unique={};new_u=[];new_v=[];mapping=[]
    for a,b in zip(u,v):
        key=round(a,9),round(b,9)
        if key not in unique:
            unique[key]=len(new_u);new_u.append(a);new_v.append(b)
        mapping.append(unique[key])
    fn.clearUVs();fn.setUVs(new_u,new_v);fn.assignUVs(uvcounts,[mapping[i] for i in uvids])
    neighbors={};ptr=0
    for face,count in enumerate(counts):
        polygon=list(vertices[ptr:ptr+count]);ptr+=count
        for a,b in zip(polygon,polygon[1:]+polygon[:1]):neighbors.setdefault(frozenset((a,b)),[]).append(face)
    for edge in range(fn.numEdges):
        faces=neighbors[frozenset(fn.getEdgeVertices(edge))]
        cap_neighbors=sum(f in cap_faces for f in faces)
        fn.setEdgeSmoothing(edge,not(cap_neighbors==1))
    fn.cleanupEdgeSmoothing()
    return output


class ResetTubeNode(om.MPxNode):
    name='modelPackResetTube';id=om.MTypeId(0x0013B2F2)
    @staticmethod
    def creator():return ResetTubeNode()
    @staticmethod
    def initialize():
        cls=ResetTubeNode;inputs=[_mesh_attr(cls,'inputMesh','in')]
        _mesh_attr(cls,'outputMesh','out',True)
        inputs.append(_numeric(cls,'sides','sides',om.MFnNumericData.kInt,6))
        typed=om.MFnTypedAttribute();cls.seam=typed.create('seamEdges','seam',om.MFnData.kString)
        cls.addAttribute(cls.seam);inputs.append(cls.seam)
        matrix=om.MFnMatrixAttribute();cls.matrix=matrix.create('meshMatrix','mat');cls.addAttribute(cls.matrix);inputs.append(cls.matrix)
        for attr in inputs:cls.attributeAffects(attr,cls.outputMesh)
    def compute(self,plug,data):
        cls=type(self)
        if plug!=cls.outputMesh:return None
        source=data.inputValue(cls.inputMesh).asMesh()
        if source.isNull():return
        from tube_resection import _resection_data
        fn=om.MFnDependencyNode(self.thisMObject())
        segments=data.inputValue(fn.attribute('lengthSegments')).asInt() if fn.hasAttribute('lengthSegments') else 0
        angle=None;extra=0
        if fn.hasAttribute('reduceByAngle'):
            segments=0
            if data.inputValue(fn.attribute('reduceByAngle')).asBool():angle=data.inputValue(fn.attribute('reductionAngle')).asDouble()
            extra=data.inputValue(fn.attribute('extraLoops')).asInt()
        output,_,_,_,caps,_=_resection_data(source,json.loads(data.inputValue(cls.seam).asString()),
                                           data.inputValue(cls.sides).asInt(),matrix=data.inputValue(cls.matrix).asMatrix(),length_segments=segments,reduction_angle=angle,extra_loops=extra)
        _finish(data,plug,cls.outputMesh,_finish_tube_mesh(output,caps))

    def setDependentsDirty(self,plug,affected):
        if om.MFnAttribute(plug.attribute()).name in ('lengthSegments','reduceByAngle','reductionAngle','extraLoops'):
            affected.append(om.MPlug(self.thisMObject(),type(self).outputMesh))


class SlideResultNode(om.MPxNode):
    name='modelPackSlideResult';id=om.MTypeId(0x0013B2F4)
    @staticmethod
    def creator():return SlideResultNode()
    @staticmethod
    def initialize():
        cls=SlideResultNode
        _mesh_attr(cls,'inputMesh','in')
        _mesh_attr(cls,'outputMesh','out',True)
        typed=om.MFnTypedAttribute();cls.result=typed.create('resultMesh','result',om.MFnData.kMesh)
        typed.storable=True;typed.hidden=True;cls.addAttribute(cls.result)
        cls.attributeAffects(cls.result,cls.outputMesh)
        cls.attributeAffects(cls.inputMesh,cls.outputMesh)
    def compute(self,plug,data):
        cls=type(self)
        if plug!=cls.outputMesh:return None
        result=data.inputValue(cls.result).asMesh()
        if result.isNull():result=data.inputValue(cls.inputMesh).asMesh()
        _finish(data,plug,cls.outputMesh,result)


class CylinderCapsNode(om.MPxNode):
    name='modelPackCylinderCaps';id=om.MTypeId(0x0013B2F5)
    @staticmethod
    def creator():return CylinderCapsNode()
    @staticmethod
    def initialize():
        cls=CylinderCapsNode
        _mesh_attr(cls,'inputMesh','in')
        _mesh_attr(cls,'outputMesh','out',True)
        enum=om.MFnEnumAttribute();cls.capType=enum.create('capType','cap',0)
        for value,label in enumerate(('Cap','Center','Parallel')):enum.addField(label,value)
        enum.keyable=True;cls.addAttribute(cls.capType)
        cls.attributeAffects(cls.inputMesh,cls.outputMesh)
        cls.attributeAffects(cls.capType,cls.outputMesh)
    def compute(self,plug,data):
        cls=type(self)
        if plug!=cls.outputMesh:return None
        source=data.inputValue(cls.inputMesh).asMesh()
        if source.isNull():return
        from model_pack_draft.cylinder_caps import evaluate
        output=evaluate(source,data.inputValue(cls.capType).asShort())
        _finish(data,plug,cls.outputMesh,output)


class SetSlideResult(om.MPxCommand):
    @staticmethod
    def creator():return SetSlideResult()
    def isUndoable(self):return True
    def doIt(self,args):
        from slide_for_maya.edit_session import PENDING
        sel=om.MSelectionList();sel.add(args.asString(0))
        self.plug=om.MFnDependencyNode(sel.getDependNode(0)).findPlug('resultMesh',False)
        try:self.before=self.plug.asMObject()
        except RuntimeError:self.before=om.MFnMeshData().create()
        self.after=PENDING[args.asString(1)]
        self.redoIt()
    def redoIt(self):self.plug.setMObject(self.after)
    def undoIt(self):self.plug.setMObject(self.before)


def initializePlugin(plugin):
    fn=om.MFnPlugin(plugin,'Model Pack','1.0','Any')
    for cls in (ResetTubeNode,SlideResultNode,CylinderCapsNode):fn.registerNode(cls.name,cls.id,cls.creator,cls.initialize)
    fn.registerCommand('modelPackSetSlideResult',SetSlideResult.creator)
    from model_pack_mirror_session import MirrorEditCommand,MirrorApplyCommand
    fn.registerCommand('modelPackMirrorEdit',MirrorEditCommand.creator)
    fn.registerCommand('modelPackApplyMirror',MirrorApplyCommand.creator)


def uninitializePlugin(plugin):
    fn=om.MFnPlugin(plugin)
    fn.deregisterCommand('modelPackSetSlideResult')
    fn.deregisterCommand('modelPackMirrorEdit')
    fn.deregisterCommand('modelPackApplyMirror')
    for cls in (CylinderCapsNode,ResetTubeNode,SlideResultNode):fn.deregisterNode(cls.id)
