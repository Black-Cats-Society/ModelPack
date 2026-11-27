"""Persistent Surface Fit geometry; all settings use native undoable Maya plugs."""
import json
from pathlib import Path
import sys
root=str(Path(globals().get('__file__',sys._getframe().f_code.co_filename)).resolve().parent)
if root not in sys.path:sys.path.insert(0,root)
from maya.api import OpenMaya as om
import model_pack_surface_fit as geometry

def maya_useNewAPI():pass

class SurfaceFit(om.MPxNode):
    name='modelPackSurfaceFit';type_id=om.MTypeId(0x0013B310)
    def __init__(self):
        super().__init__();self.last_good=None;self.edge_cache=None
    @staticmethod
    def creator():return SurfaceFit()
    @staticmethod
    def initialize():
        c=SurfaceFit;inputs=[]
        for name,output in (('inputMesh',False),('surface',False),('outputMesh',True)):
            a=om.MFnTypedAttribute();attr=a.create(name,name,om.MFnData.kMesh)
            a.storable=False;a.hidden=True;a.writable=not output;c.addAttribute(attr);setattr(c,name,attr)
            if not output:inputs.append(attr)
        a=om.MFnMatrixAttribute();c.sourceMatrix=a.create('sourceMatrix','sourceMatrix');c.addAttribute(c.sourceMatrix);inputs.append(c.sourceMatrix)
        for name,default in (('axis',1),('scale',1.),('offset',0.),('face',-1),('triangle',0)):
            a=om.MFnNumericAttribute();kind=om.MFnNumericData.kInt if isinstance(default,int) else om.MFnNumericData.kDouble
            attr=a.create(name,name,kind,default);a.keyable=True;c.addAttribute(attr);setattr(c,name,attr);inputs.append(attr)
        for name,default in (('anchor',(0.,0.,0.)),('normal',(0.,1.,0.)),('tangent',(1.,0.,0.)),('weights',(1.,0.,0.))):
            a=om.MFnNumericAttribute();attr=a.createPoint(name,name);a.default=default
            c.addAttribute(attr);setattr(c,name,attr);inputs.append(attr)
        a=om.MFnTypedAttribute();c.ramp=a.create('ramp','ramp',om.MFnData.kString,om.MFnStringData().create('[[0,1],[1,0]]'))
        c.addAttribute(c.ramp);inputs.append(c.ramp)
        a=om.MFnNumericAttribute();c.valid=a.create('valid','valid',om.MFnNumericData.kBoolean,True);a.writable=False;a.storable=False;c.addAttribute(c.valid)
        a=om.MFnTypedAttribute();c.status=a.create('status','status',om.MFnData.kString);a.writable=False;a.storable=False;c.addAttribute(c.status)
        for attr in inputs:
            for out in (c.outputMesh,c.valid,c.status):c.attributeAffects(attr,out)
    def setDependentsDirty(self,plug,affected):
        name=plug.partialName(useLongNames=True)
        # The cache key includes geometry, topology and axis. A DAG transform
        # can dirty unchanged input plugs; do not discard a still-valid field.
        if name in ('edgeMode','surfaceBinding','outputFrame'):
            for out in (type(self).outputMesh,type(self).valid,type(self).status):affected.append(om.MPlug(self.thisMObject(),out))
    def compute(self,plug,data):
        c=type(self)
        if plug.attribute() not in (c.outputMesh,c.valid,c.status):return om.kUnknownParameter
        original=data.inputValue(c.inputMesh).asMesh()
        if original.isNull():return
        matrix=data.inputValue(c.sourceMatrix).asMatrix();source=om.MFnMesh(original)
        rest=[p*matrix for p in source.getPoints()];output_points=rest;valid=True;message='Выберите поверхность в viewport'
        # worldMesh carries its transform separately. asMesh() still exposes
        # local coordinates; the gizmo uses world coordinates from the DAG.
        surface_handle=data.inputValue(c.surface)
        raw_surface=surface_handle.asMesh()
        surface=surface_handle.asMeshTransformed()
        try:
            face=data.inputValue(c.face).asInt()
            connected=om.MPlug(self.thisMObject(),c.surface).isDestination
            if (surface.isNull() or not connected) and face>=0:raise ValueError('Поверхность удалена. Выберите другую поверхность.')
            if connected and not surface.isNull() and face>=0:
                target=om.MFnMesh(surface);anchor=om.MVector(data.inputValue(c.anchor).asFloat3())
                normal=om.MVector(data.inputValue(c.normal).asFloat3());tangent=om.MVector(data.inputValue(c.tangent).asFloat3())
                face=data.inputValue(c.face).asInt();tri=data.inputValue(c.triangle).asInt()
                dependency=om.MFnDependencyNode(self.thisMObject())
                binding=data.inputValue(dependency.findPlug('surfaceBinding',False).attribute()).asString() if dependency.hasAttribute('surfaceBinding') else ''
                raw_target=om.MFnMesh(raw_surface)
                rebound=geometry.rebound_contact(raw_target,binding)
                if rebound is not None:
                    local_point,local_normal=rebound
                    anchor=om.MVector(local_point*surface_handle.geometryTransformMatrix())
                    normal=(local_normal*surface_handle.geometryTransformMatrix().inverse().transpose()).normal()
                elif face>=0:
                    ids=target.getPolygonTriangleVertices(face,tri);weights=data.inputValue(c.weights).asFloat3()
                    anchor=sum((om.MVector(target.getPoint(v))*weights[i] for i,v in enumerate(ids)),om.MVector())
                    raw_target=om.MFnMesh(raw_surface)
                    normal=sum((raw_target.getFaceVertexNormal(face,v)*weights[i] for i,v in enumerate(ids)),om.MVector())
                    normal=(normal*surface_handle.geometryTransformMatrix().inverse().transpose()).normal()
                axis=list(geometry.AXES)[data.inputValue(c.axis).asInt()]
                u,n,v=geometry.source_frame(axis,matrix)
                dependency=om.MFnDependencyNode(self.thisMObject())
                edge_mode=dependency.hasAttribute('edgeMode') and data.inputValue(dependency.findPlug('edgeMode',False).attribute()).asBool()
                profile=None
                if edge_mode:
                    cached=getattr(self,'edge_cache',None)
                    key=geometry.edge_profile_key(rest,n,source)
                    if cached is None or cached[0]!=key:
                        cached=(key,geometry.open_edge_profile(rest,source,n));self.edge_cache=cached
                    profile=cached[1]
                knots=sorted(json.loads(data.inputValue(c.ramp).asString()))
                def ramp(t):
                    for (a,x),(b,y) in zip(knots,knots[1:]):
                        if t<=b:
                            s=max(0.,min(1.,(t-a)/max(b-a,1e-9)));s=s*s*(3-2*s)
                            return x+(y-x)*s
                    return knots[-1][1]
                output_points=geometry.fit(rest,target,anchor,normal,axis,data.inputValue(c.scale).asDouble(),
                    data.inputValue(c.offset).asDouble(),ramp,frame=(u,n,v),tangent=tangent,space=om.MSpace.kObject,
                    normal_matrix=surface_handle.geometryTransformMatrix().inverse().transpose(),edge_profile=profile)
                message='Прилегание: '+str(len(output_points))+' вершин'
                if edge_mode and profile is None:message+=' · край не найден, градиент по высоте'
            elif face<0:
                axis=list(geometry.AXES)[data.inputValue(c.axis).asInt()]
                output_points=geometry.preview(rest,axis,matrix,data.inputValue(c.scale).asDouble(),data.inputValue(c.offset).asDouble())
            dependency=om.MFnDependencyNode(self.thisMObject())
            if dependency.hasAttribute('outputFrame'):
                frame_data=data.inputValue(dependency.findPlug('outputFrame',False).attribute()).data()
                inverse=om.MFnMatrixData(frame_data).matrix().inverse()
                output_points=[point*inverse for point in output_points]
            result=om.MFnMeshData().create();obj=om.MFnMesh().copy(original,result);om.MFnMesh(obj).setPoints(output_points)
            self.last_good=result
        except (ValueError,RuntimeError,IndexError) as error:
            import traceback
            self.last_error=traceback.format_exc()
            valid=False;message=str(error)
            result=self.last_good
            if result is None:
                fallback=rest;dependency=om.MFnDependencyNode(self.thisMObject())
                if dependency.hasAttribute('outputFrame'):
                    frame_data=data.inputValue(dependency.findPlug('outputFrame',False).attribute()).data()
                    inverse=om.MFnMatrixData(frame_data).matrix().inverse();fallback=[point*inverse for point in rest]
                result=om.MFnMeshData().create();obj=om.MFnMesh().copy(original,result);om.MFnMesh(obj).setPoints(fallback)
        data.outputValue(c.outputMesh).setMObject(result)
        data.outputValue(c.valid).setBool(valid);data.outputValue(c.status).setString(message)
        for out in (c.outputMesh,c.valid,c.status):data.outputValue(out).setClean()

def initializePlugin(obj):om.MFnPlugin(obj,'Black Cats Society','0.1.10','Any').registerNode(SurfaceFit.name,SurfaceFit.type_id,SurfaceFit.creator,SurfaceFit.initialize)
def uninitializePlugin(obj):om.MFnPlugin(obj).deregisterNode(SurfaceFit.type_id)
