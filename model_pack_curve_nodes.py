"""Persistent Control Curve history. Scene evaluation needs no active toolbox."""
import sys
from pathlib import Path
root=str(Path(globals().get('__file__',sys._getframe().f_code.co_filename)).resolve().parent)
if root not in sys.path:sys.path.insert(0,root)
from maya.api import OpenMaya as om


def maya_useNewAPI():pass


class ControlCurveNode(om.MPxNode):
    name='modelPackControlCurve';id=om.MTypeId(0x0013B2F6)

    @staticmethod
    def creator():return ControlCurveNode()

    @staticmethod
    def initialize():
        cls=ControlCurveNode;inputs=[]
        for name,short,kind,output,array in (
                ('inputMesh','in',om.MFnData.kMesh,False,False),
                ('outputMesh','out',om.MFnData.kMesh,True,False),
                ('binding','bind',om.MFnData.kString,False,False),
                ('controlCurve','cc',om.MFnData.kNurbsCurve,False,True)):
            attr=om.MFnTypedAttribute();value=attr.create(name,short,kind)
            attr.hidden=True;attr.storable=name=='binding';attr.writable=not output
            attr.array=array;attr.usesArrayDataBuilder=array
            if name=='controlCurve':attr.disconnectBehavior=om.MFnAttribute.kReset
            cls.addAttribute(value);setattr(cls,name,value)
            if not output:inputs.append(value)
        for name,short,array in (('curveMatrix','cm',True),('meshInverseMatrix','mim',False)):
            attr=om.MFnMatrixAttribute();value=attr.create(name,short)
            attr.hidden=True;attr.storable=False;attr.array=array;attr.usesArrayDataBuilder=array
            cls.addAttribute(value);setattr(cls,name,value);inputs.append(value)
        attr=om.MFnNumericAttribute();cls.envelope=attr.create('envelope','env',om.MFnNumericData.kDouble,1.)
        attr.keyable=True;attr.setMin(0.);attr.setMax(1.);cls.addAttribute(cls.envelope);inputs.append(cls.envelope)
        for value in inputs:cls.attributeAffects(value,cls.outputMesh)

    def compute(self,plug,data):
        cls=type(self)
        if plug!=cls.outputMesh:return None
        source=data.inputValue(cls.inputMesh).asMesh()
        if source.isNull():return
        from recurve_saved_curves import prepare,evaluate
        text=data.inputValue(cls.binding).asString()
        if getattr(self,'binding_text',None)!=text:
            self.state=prepare(text);self.binding_text=text
        controls=data.inputArrayValue(cls.controlCurve);matrices=data.inputArrayValue(cls.curveMatrix)
        inverse=data.inputValue(cls.meshInverseMatrix).asMatrix();curves=[];relative=[]
        inputs=om.MPlug(self.thisMObject(),cls.controlCurve)
        for index in range(len(self.state['rows'])):
            # Maya can retain cached NURBS data after its source is deleted.
            # A disconnected guide must not keep deforming from that cache.
            if not inputs.elementByLogicalIndex(index).isDestination:
                curves.append(None);relative.append(om.MMatrix());continue
            try:
                controls.jumpToLogicalElement(index);curve=controls.inputValue().asNurbsCurve()
                matrices.jumpToLogicalElement(index);matrix=matrices.inputValue().asMatrix()*inverse
                curves.append(None if curve.isNull() else curve);relative.append(matrix)
            except RuntimeError:curves.append(None);relative.append(om.MMatrix())
        result=evaluate(source,self.state,curves,relative,data.inputValue(cls.envelope).asDouble(), om.MFnDependencyNode(self.thisMObject()))
        data.outputValue(cls.outputMesh).setMObject(result);data.setClean(plug)


def initializePlugin(plugin):
    cls=ControlCurveNode
    om.MFnPlugin(plugin,'Model Pack','1.0','Any').registerNode(cls.name,cls.id,cls.creator,cls.initialize)


def uninitializePlugin(plugin):
    om.MFnPlugin(plugin).deregisterNode(ControlCurveNode.id)
