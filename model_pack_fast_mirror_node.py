"""API 1 node: data-only UV-set creation is unavailable in Python API 2."""
import sys
from pathlib import Path
root=str(Path(globals().get('__file__',sys._getframe().f_code.co_filename)).resolve().parent)
if root not in sys.path:sys.path.insert(0,root)
from maya import OpenMaya as om,OpenMayaMPx as ompx
from maya.api import OpenMaya as math_api
import model_pack_legacy_mesh as backend
from model_pack_mirror_geometry import build

NAME='modelPackMirrorCut';ID=om.MTypeId(0x0013B2F3)


class MirrorNode(ompx.MPxNode):
    def __init__(self):super().__init__();backend.REGISTERED_NODE_CLASSES.add(type(self))
    @staticmethod
    def creator():return ompx.asMPxPtr(MirrorNode())
    @staticmethod
    def initialize():
        cls=MirrorNode;typed=om.MFnTypedAttribute()
        cls.inputMesh=typed.create('inputMesh','in',om.MFnData.kMesh);typed.setStorable(False);cls.addAttribute(cls.inputMesh)
        cls.outputMesh=typed.create('outputMesh','out',om.MFnData.kMesh)
        typed.setStorable(False);typed.setWritable(False);cls.addAttribute(cls.outputMesh)
        inputs=[cls.inputMesh];numeric=om.MFnNumericAttribute()
        for name,short,kind,default in (('axis','axis',om.MFnNumericData.kInt,0),('space','sp',om.MFnNumericData.kInt,0),
                ('direction','dir',om.MFnNumericData.kInt,0),('threshold','thr',om.MFnNumericData.kDouble,.001)):
            attr=numeric.create(name,short,kind,default);numeric.setKeyable(True);cls.addAttribute(attr)
            setattr(cls,name,attr);inputs.append(attr)
        cls.pivot_children=[numeric.create('pivot'+s,'p'+s.lower(),om.MFnNumericData.kDouble,0.) for s in 'XYZ']
        cls.pivot=numeric.create('pivot','piv',*cls.pivot_children);cls.addAttribute(cls.pivot);inputs.append(cls.pivot)
        matrix=om.MFnMatrixAttribute();cls.matrix=matrix.create('meshMatrix','mat');cls.addAttribute(cls.matrix);inputs.append(cls.matrix)
        for attr in inputs:cls.attributeAffects(attr,cls.outputMesh)
    def compute(self,plug,data):
        # An already registered older class can have different Python attribute
        # fields. Resolve Maya's actual attributes, including compound children.
        fn=om.MFnDependencyNode(self.thisMObject())
        output_attr=fn.attribute('outputMesh')
        if plug!=output_attr:return om.kUnknownParameter
        handle=data.inputValue(fn.attribute('inputMesh'));source=handle.asMesh()
        if source.isNull():return
        matrix_handle=data.inputValue(fn.attribute('meshMatrix'));matrix=matrix_handle.asMatrix()
        values=[om.MScriptUtil.getDoubleArrayItem(matrix[row],column) for row in range(4) for column in range(4)]
        space=data.inputValue(fn.attribute('space')).asInt()
        pivot_plug=fn.findPlug('pivot',False)
        pivot=[data.inputValue(pivot_plug.child(i).attribute()).asDouble() for i in range(3)]
        frame=math_api.MMatrix(values);pivot_in_frame=False
        if space==1 and fn.hasAttribute('pivotFrameMatrix'):
            rotation_handle=data.inputValue(fn.attribute('pivotFrameMatrix'))
            rotation_data=om.MFnMatrixData(rotation_handle.data());rotation=rotation_data.matrix()
            rotation=math_api.MMatrix([om.MScriptUtil.getDoubleArrayItem(rotation[row],column) for row in range(4) for column in range(4)])
            def vector(name):
                plug=fn.findPlug(name,False)
                return math_api.MVector(*(data.inputValue(plug.child(i).attribute()).asDouble() for i in range(3)))
            sp,st=vector('scalePivot'),vector('scalePivotTranslate')
            translation=(-sp)*frame+(sp+st)*rotation
            values[12:15]=list(translation)
            frame=math_api.MMatrix(values)
            center=math_api.MPoint(*pivot);center*=rotation
            pivot=(center.x,center.y,center.z);pivot_in_frame=True
        if fn.hasAttribute('modelPackPlaneEnabled') and data.inputValue(fn.attribute('modelPackPlaneEnabled')).asBool():
            def read_matrix(name):
                value=om.MFnMatrixData(data.inputValue(fn.attribute(name)).data()).matrix()
                return math_api.MMatrix([om.MScriptUtil.getDoubleArrayItem(value[row],column) for row in range(4) for column in range(4)])
            frame=read_matrix('modelPackPlaneMeshMatrix')*read_matrix('modelPackPlaneMatrix').inverse()
            pivot=(0.,0.,0.);pivot_in_frame=True;space=0
        output=build(source,handle.data(),data.inputValue(fn.attribute('axis')).asInt(),space,
                     data.inputValue(fn.attribute('direction')).asInt(),pivot,
                     data.inputValue(fn.attribute('threshold')).asDouble(),frame,backend=backend,pivot_in_frame=pivot_in_frame)
        data.outputValue(output_attr).setMObject(output);data.setClean(plug)
    def setDependentsDirty(self,plug,affected):
        name=om.MFnAttribute(plug.attribute()).name()
        if name=='pivotFrameMatrix' or name.startswith('scalePivot') or name.startswith('modelPackPlane'):
            fn=om.MFnDependencyNode(self.thisMObject())
            affected.append(om.MPlug(self.thisMObject(),fn.attribute('outputMesh')))


def initializePlugin(obj):ompx.MFnPlugin(obj,'Model Pack','1.0','Any').registerNode(NAME,ID,MirrorNode.creator,MirrorNode.initialize)
def uninitializePlugin(obj):ompx.MFnPlugin(obj).deregisterNode(ID)
