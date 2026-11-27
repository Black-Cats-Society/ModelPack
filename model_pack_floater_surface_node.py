"""Optional native surface binding, compatible with already loaded Floater nodes."""
import sys
from pathlib import Path
root=str(Path(globals().get('__file__',sys._getframe().f_code.co_filename)).resolve().parent)
if root not in sys.path:sys.path.insert(0,root)
from maya.api import OpenMaya as om
from model_pack_mesh_safety import register_cache,remember,recover,direction_attribute,is_flipped
from model_pack_floater_geometry import sample,mesh
from model_pack_floater_surface import Surface,build

NAME='modelPackFloaterSurface';ID=om.MTypeId(0x0013B2F9)
def maya_useNewAPI():pass


class SurfaceNode(om.MPxNode):
    def __init__(self):super().__init__();self.last_plan=None
    @staticmethod
    def creator():return SurfaceNode()
    @staticmethod
    def initialize():
        cls=SurfaceNode;register_cache(cls);inputs=[];outputs=[];typed=om.MFnTypedAttribute()
        for name,short,kind in (('inputCurve','curve',om.MFnData.kNurbsCurve),('surfaceMesh','surface',om.MFnData.kMesh)):
            attr=typed.create(name,short,kind);typed.hidden=True;typed.storable=False;cls.addAttribute(attr);setattr(cls,name,attr);inputs.append(attr)
        cls.output=typed.create('outputMesh','out',om.MFnData.kMesh);typed.writable=False;typed.storable=False;cls.addAttribute(cls.output);outputs.append(cls.output)
        cls.status=typed.create('status','status',om.MFnData.kString);typed.writable=False;typed.storable=False;cls.addAttribute(cls.status);outputs.append(cls.status)
        numeric=om.MFnNumericAttribute();cls.valid=numeric.create('valid','valid',om.MFnNumericData.kBoolean,False)
        numeric.writable=False;numeric.storable=False;cls.addAttribute(cls.valid);outputs.append(cls.valid)
        matrix=om.MFnMatrixAttribute();cls.matrix=matrix.create('curveMatrix','matrix');matrix.hidden=True;cls.addAttribute(cls.matrix);inputs.append(cls.matrix)
        unit=om.MFnUnitAttribute()
        for name,short,value in (('openingWidth','width',.1),('height','height',.03),('skirtLength','skirt',.1)):
            attr=unit.create(name,short,om.MFnUnitAttribute.kDistance,value);unit.hidden=True
            cls.addAttribute(attr);setattr(cls,name,attr);inputs.append(attr)
        direction=direction_attribute();cls.addAttribute(direction);inputs.append(direction)
        for source in inputs:
            for target in outputs:cls.attributeAffects(source,target)
    def compute(self,plug,data):
        cls=type(self)
        if not any(plug==attr for attr in (cls.output,cls.status,cls.valid)):return None
        status='';valid=False;output=None
        try:
            for attr,label in ((cls.inputCurve,'исходная кривая'),(cls.surfaceMesh,'опорная поверхность')):
                if not om.MPlug(self.thisMObject(),attr).isDestination:raise ValueError('Floater: '+label+' удалена.')
            curve=om.MFnNurbsCurve(data.inputValue(cls.inputCurve).asNurbsCurve());matrix=data.inputValue(cls.matrix).asMatrix()
            parameters=[data.inputValue(getattr(cls,n)).asDistance().asCentimeters() for n in ('openingWidth','height','skirtLength')]
            points,closed=sample(curve,matrix,parameters[0]);surface=Surface(data.inputValue(cls.surfaceMesh).asMesh())
            plan=build(points,closed,*parameters,surface,reverse=is_flipped(self.thisMObject(),data))
            output=remember(self.thisMObject(),data,mesh(plan));self.last_plan=plan;valid=True
        except (ValueError,RuntimeError,TypeError,OverflowError) as error:status=str(error)
        if not valid:output=recover(self.thisMObject(),data)
        data.outputValue(cls.output).setMObject(output)
        data.outputValue(cls.status).setString(status);data.outputValue(cls.valid).setBool(valid)
        for attr in (cls.output,cls.status,cls.valid):data.setClean(om.MPlug(self.thisMObject(),attr))

    def setDependentsDirty(self,plug,affected):
        if om.MFnAttribute(plug.attribute()).name=='flipDirection':
            cls=type(self)
            for attr in (cls.output,cls.status,cls.valid):affected.append(om.MPlug(self.thisMObject(),attr))


def initializePlugin(plugin):om.MFnPlugin(plugin,'Model Pack','1.0','Any').registerNode(NAME,ID,SurfaceNode.creator,SurfaceNode.initialize)
def uninitializePlugin(plugin):om.MFnPlugin(plugin).deregisterNode(ID)
