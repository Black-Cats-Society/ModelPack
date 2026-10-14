"""Mesh-data adapter for API 1's data-only UV-set creation method."""
from maya import OpenMaya as old
from maya.api import OpenMaya as om

REGISTERED_NODE_CLASSES=globals().get('REGISTERED_NODE_CLASSES',set())


def ints(values):
    result=old.MIntArray()
    old.MScriptUtil.createIntArrayFromList(list(values),result)
    return result


def floats(values):
    result=old.MFloatArray()
    old.MScriptUtil.createFloatArrayFromList(list(values),result)
    return result


class Mesh:
    def __init__(self,obj=None):
        self.fn=old.MFnMesh(obj) if obj is not None else old.MFnMesh()
        self.util=old.MScriptUtil();self.util.createFromList([0,0],2);self.edge_pointer=self.util.asInt2Ptr()
    @property
    def numPolygons(self):return self.fn.numPolygons()
    @property
    def numEdges(self):return self.fn.numEdges()
    def getPoints(self):
        result=old.MPointArray();self.fn.getPoints(result)
        points=[]
        for i in range(result.length()):
            point=result[i];points.append(om.MPoint(point.x,point.y,point.z))
        return points
    def getVertices(self):
        counts=old.MIntArray();vertices=old.MIntArray();self.fn.getVertices(counts,vertices)
        return list(counts),list(vertices)
    def getTriangles(self):
        counts=old.MIntArray();vertices=old.MIntArray();self.fn.getTriangles(counts,vertices)
        return list(counts),list(vertices)
    def getPolygonTriangleVertices(self,face,triangle):
        counts,vertices=self.getTriangles();start=(sum(counts[:face])+triangle)*3
        return vertices[start:start+3]
    def getUVSetNames(self):
        result=[];self.fn.getUVSetNames(result);return result
    def getUVs(self,name):
        u,v=old.MFloatArray(),old.MFloatArray();self.fn.getUVs(u,v,name);return list(u),list(v)
    def getAssignedUVs(self,name):
        counts=old.MIntArray();ids=old.MIntArray();self.fn.getAssignedUVs(counts,ids,name);return list(counts),list(ids)
    def create(self,points,counts,vertices,parent):
        array=old.MPointArray()
        for point in points:array.append(old.MPoint(point.x,point.y,point.z))
        self.fn.create(len(points),len(counts),array,ints(counts),ints(vertices),parent)
    def createUVSet(self,name):return self.fn.createUVSetDataMeshWithName(name)
    def setUVs(self,u,v,name):self.fn.setUVs(floats(u),floats(v),name)
    def assignUVs(self,counts,ids,name):self.fn.assignUVs(ints(counts),ints(ids),name)
    def getEdgeVertices(self,edge):
        self.fn.getEdgeVertices(edge,self.edge_pointer)
        return tuple(old.MScriptUtil.getInt2ArrayItem(self.edge_pointer,0,i) for i in (0,1))
    def isEdgeSmooth(self,edge):return self.fn.isEdgeSmooth(edge)
    def setEdgeSmoothing(self,edge,smooth):self.fn.setEdgeSmoothing(edge,smooth)
    def cleanupEdgeSmoothing(self):self.fn.cleanupEdgeSmoothing()
    def getPolygonNormal(self,face):
        normal=old.MVector();self.fn.getPolygonNormal(face,normal);return om.MVector(normal.x,normal.y,normal.z)
    def lockedNormals(self):
        flags=[self.fn.isNormalLocked(i) for i in range(self.fn.numNormals())]
        if not any(flags):return None
        values=old.MFloatVectorArray();self.fn.getNormals(values)
        vectors=[om.MVector(values[i].x,values[i].y,values[i].z) for i in range(values.length())]
        counts,ids=old.MIntArray(),old.MIntArray();self.fn.getNormalIds(counts,ids)
        return flags,vectors,list(ids)
    def setLockedNormals(self,normals,faces,vertices):
        values=old.MVectorArray()
        for normal in normals:values.append(old.MVector(normal.x,normal.y,normal.z))
        self.fn.setFaceVertexNormals(values,ints(faces),ints(vertices),old.MSpace.kObject)
        self.fn.lockFaceVertexNormals(ints(faces),ints(vertices))
    def colorSets(self):
        names=[];self.fn.getColorSetNames(names);return names
    def colors(self,name):
        values=old.MColorArray();self.fn.getFaceVertexColors(values,name)
        return [(values[i].r,values[i].g,values[i].b,values[i].a) for i in range(values.length())]
    def setColors(self,name,colors,faces,vertices):
        self.fn.createColorSetDataMesh(name)
        values=old.MColorArray()
        for color in colors:values.append(old.MColor(*color))
        self.fn.setColors(values,name)
        self.fn.assignColors(ints(range(len(colors))),name)


class Data:
    def __init__(self,obj=None):self.fn=old.MFnMeshData(obj) if obj is not None else old.MFnMeshData()
    def create(self):return self.fn.create()
    def copyObjectGroups(self,obj):self.fn.copyObjectGroups(obj)
    @property
    def objectGroupCount(self):return self.fn.objectGroupCount()
    def objectGroup(self,index):return self.fn.objectGroup(index)
    def objectGroupComponent(self,index):return self.fn.objectGroupComponent(index)
    def setObjectGroupComponent(self,index,component):self.fn.setObjectGroupComponent(index,component)


class Component:
    def __init__(self,obj=None):self.fn=old.MFnSingleIndexedComponent(obj) if obj is not None else old.MFnSingleIndexedComponent()
    def create(self,kind):return self.fn.create(kind)
    @property
    def isComplete(self):return self.fn.isComplete()
    def getElements(self):
        values=old.MIntArray();self.fn.getElements(values);return list(values)
    def addElements(self,values):self.fn.addElements(ints(values))
