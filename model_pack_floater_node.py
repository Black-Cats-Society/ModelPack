"""Editable curve-driven recessed floater mesh, with native scene history."""
import sys
from pathlib import Path
root=str(Path(globals().get('__file__',sys._getframe().f_code.co_filename)).resolve().parent)
if root not in sys.path:sys.path.insert(0,root)
from maya.api import OpenMaya as om
from model_pack_mesh_safety import register_cache,remember,recover,direction_attribute,is_flipped
from model_pack_floater_geometry import sample,build,mesh

NAME='modelPackFloater';ID=om.MTypeId(0x0013B2F8)


def maya_useNewAPI():pass


class FloaterNode(om.MPxNode):
    def __init__(self):super().__init__();self.last_plan=None
    @staticmethod
    def creator():return FloaterNode()
    @staticmethod
    def initialize():
        cls=FloaterNode;register_cache(cls);inputs=[];outputs=[];typed=om.MFnTypedAttribute()
        cls.curve=typed.create('inputCurve','curve',om.MFnData.kNurbsCurve);typed.hidden=True;typed.storable=False;cls.addAttribute(cls.curve);inputs.append(cls.curve)
        cls.output=typed.create('outputMesh','out',om.MFnData.kMesh);typed.writable=False;typed.storable=False;cls.addAttribute(cls.output);outputs.append(cls.output)
        cls.status=typed.create('status','status',om.MFnData.kString);typed.writable=False;typed.storable=False;cls.addAttribute(cls.status);outputs.append(cls.status)
        numeric=om.MFnNumericAttribute();cls.valid=numeric.create('valid','valid',om.MFnNumericData.kBoolean,False)
        numeric.writable=False;numeric.storable=False;cls.addAttribute(cls.valid);outputs.append(cls.valid)
        unit=om.MFnUnitAttribute()
        for name,short,value in (('openingWidth','width',.1),('height','height',.03),('skirtLength','skirt',.1)):
            attr=unit.create(name,short,om.MFnUnitAttribute.kDistance,value);unit.keyable=True;unit.setMin(1e-7 if name=='openingWidth' else 0.)
            cls.addAttribute(attr);setattr(cls,name,attr);inputs.append(attr)
        matrix=om.MFnMatrixAttribute();cls.matrix=matrix.create('curveMatrix','matrix');matrix.hidden=True;cls.addAttribute(cls.matrix);inputs.append(cls.matrix)
        numeric=om.MFnNumericAttribute();cls.normal=numeric.createPoint('normalHint','normal');numeric.hidden=True;numeric.default=(0,1,0);cls.addAttribute(cls.normal);inputs.append(cls.normal)
        direction=direction_attribute();cls.addAttribute(direction);inputs.append(direction)
        for source in inputs:
            for target in outputs:cls.attributeAffects(source,target)
    def compute(self,plug,data):
        cls=type(self)
        if not any(plug==attr for attr in (cls.output,cls.status,cls.valid)):return None
        status='';valid=False;output=None
        try:
            source=data.inputValue(cls.curve).asNurbsCurve()
            if source.isNull() or not om.MPlug(self.thisMObject(),cls.curve).isDestination:raise ValueError('Floater: исходная кривая удалена.')
            curve=om.MFnNurbsCurve(source);matrix=data.inputValue(cls.matrix).asMatrix()
            width,height,skirt=[data.inputValue(getattr(cls,n)).asDistance().asCentimeters() for n in ('openingWidth','height','skirtLength')]
            points,closed=sample(curve,matrix,width)
            hint=om.MVector(data.inputValue(cls.normal).asFloat3()).transformAsNormal(matrix)
            if is_flipped(self.thisMObject(),data):hint=-hint
            plan=build(points,closed,width,height,skirt,hint)
            output=remember(self.thisMObject(),data,mesh(plan));self.last_plan=plan;valid=True
        except (ValueError,RuntimeError,TypeError,OverflowError) as error:status=str(error)
        if not valid:output=recover(self.thisMObject(),data)
        data.outputValue(cls.output).setMObject(output);data.outputValue(cls.status).setString(status);data.outputValue(cls.valid).setBool(valid)
        for attr in (cls.output,cls.status,cls.valid):data.setClean(om.MPlug(self.thisMObject(),attr))

    def setDependentsDirty(self,plug,affected):
        if om.MFnAttribute(plug.attribute()).name=='flipDirection':
            cls=type(self)
            for attr in (cls.output,cls.status,cls.valid):affected.append(om.MPlug(self.thisMObject(),attr))


def initializePlugin(plugin):om.MFnPlugin(plugin,'Model Pack','1.0','Any').registerNode(NAME,ID,FloaterNode.creator,FloaterNode.initialize)
def uninitializePlugin(plugin):om.MFnPlugin(plugin).deregisterNode(ID)
