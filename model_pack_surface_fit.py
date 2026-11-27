"""Geometry kernel for reversible placement of one mesh on a surface."""
from maya.api import OpenMaya as om
import heapq
import json
import numpy as np


AXES={'x':(1,0,0),'y':(0,1,0),'z':(0,0,1),
      '-x':(-1,0,0),'-y':(0,-1,0),'-z':(0,0,-1)}


def surface_binding(fn,point):
    """Persistent object-space reference independent of polygon renumbering."""
    return json.dumps({'topology':[fn.numVertices,fn.numEdges,fn.numPolygons],
                       'point':list(point)[:3]})


def rebound_contact(fn,binding):
    """Use barycentric tracking until topology changes, then reproject the reference."""
    if not binding:return None
    saved=json.loads(binding)
    if saved['topology']==[fn.numVertices,fn.numEdges,fn.numPolygons]:return None
    return fn.getClosestPointAndNormal(om.MPoint(saved['point']),om.MSpace.kObject)[:2]


def basis(axis):
    n=om.MVector(AXES[axis])
    ref=om.MVector(0,0,1) if abs(n.z)<.9 else om.MVector(0,1,0)
    u=(n^ref).normal()
    return u,n,n^u


def coordinates(points,axis='y',frame=None):
    u,n,v=frame or basis(axis)
    values=[(om.MVector(p)*u,om.MVector(p)*n,om.MVector(p)*v) for p in points]
    if not values:raise ValueError('Select a mesh containing vertices.')
    low=min(p[1] for p in values);high=max(p[1] for p in values)
    cu=(min(p[0] for p in values)+max(p[0] for p in values))*.5
    cv=(min(p[2] for p in values)+max(p[2] for p in values))*.5
    return [(a-cu,h-low,b-cv) for a,h,b in values],max(high-low,1e-12)


def source_frame(axis,matrix):
    """The same signed source axes for the preview and geometry evaluation."""
    u,n,_=basis(axis)
    n=(n*matrix.inverse().transpose()).normal();u=(u*matrix).normal()
    v=(n^u).normal();return v^n,n,v


def support_frame(points,axis,matrix):
    """Mounting plane through the selected signed side before the first snap."""
    u,n,v=source_frame(axis,matrix)
    values=[(om.MVector(p)*u,om.MVector(p)*n,om.MVector(p)*v) for p in points]
    if not values:raise ValueError('Select a mesh containing vertices.')
    cu=(min(p[0] for p in values)+max(p[0] for p in values))*.5
    cv=(min(p[2] for p in values)+max(p[2] for p in values))*.5
    low=min(p[1] for p in values)
    return u*cu+n*low+v*cv,n,u,v


def preview(points,axis,matrix,scale=1.,offset=0.):
    """Uniform scale around the mounting pivot before any surface is chosen."""
    if scale<=0:raise ValueError('Scale must be positive.')
    center,normal,_,_=support_frame(points,axis,matrix)
    return [om.MPoint(center+(om.MVector(point)-center)*scale+normal*offset) for point in points]

def boundary_vertices(fn):
    # Geometry input data has no DAG iterator; count polygon-side incidences.
    counts,vertices=fn.getVertices();offset=0;incidence={}
    for count in counts:
        face=list(vertices[offset:offset+count]);offset+=count
        for a,b in zip(face,face[1:]+face[:1]):
            edge=(a,b) if a<b else (b,a);incidence[edge]=incidence.get(edge,0)+1
    return {v for edge,count in incidence.items() if count==1 for v in edge}

def has_open_boundary(fn):
    try:
        edges=om.MItMeshEdge(fn.dagPath())
        while not edges.isDone():
            if edges.onBoundary():return True
            edges.next()
        return False
    except RuntimeError:return bool(boundary_vertices(fn))

def edge_profile_key(points,normal,fn=None):
    topology=None
    if fn is not None:
        counts,vertices=fn.getVertices()
        topology=(fn.numVertices,fn.numEdges,fn.numPolygons,
                  hash(np.asarray(counts,dtype=np.int32).tobytes()),
                  hash(np.asarray(vertices,dtype=np.int32).tobytes()))
    return hash(np.asarray(points,dtype=float).tobytes()),tuple(normal),topology

def open_edge_profile(points,fn,normal):
    """Geodesic distance from every open border, normalised per component.

    This changes gradient distance only. The mounting height always remains
    the lowest source point along the selected signed axis.
    """
    boundary=boundary_vertices(fn)
    if not boundary:return None
    count=len(points);adj=[[] for _ in points];pairs=[];lengths=[]
    for edge in range(fn.numEdges):
        a,b=fn.getEdgeVertices(edge);length=max((points[a]-points[b]).length(),1e-12)
        adj[a].append((b,length));adj[b].append((a,length));pairs.append((a,b));lengths.append(length)
    distances=[float('inf')]*count;owners=[-1]*count;heap=[]
    for index in boundary:distances[index]=0.;owners[index]=index;heap.append((0.,index))
    heapq.heapify(heap)
    while heap:
        distance,index=heapq.heappop(heap)
        if distance>distances[index]:continue
        for other,length in adj[index]:
            proposed=distance+length
            if proposed<distances[other]:
                distances[other]=proposed;owners[other]=owners[index];heapq.heappush(heap,(proposed,other))
    parameters=[None]*count;visited=set()
    for start in range(count):
        if start in visited:continue
        component=[];stack=[start];visited.add(start)
        while stack:
            index=stack.pop();component.append(index)
            for other,_ in adj[index]:
                if other not in visited:visited.add(other);stack.append(other)
        if owners[start]<0:continue
        maximum=max(distances[i] for i in component)
        for index in component:parameters[index]=distances[index]/maximum if maximum>1e-12 else 0.
    return {'parameters':parameters,'boundary':boundary}


def fit(points,target,anchor,normal,axis='y',scale=1.,offset=0.,ramp=None,
        frame=None,tangent=None,space=om.MSpace.kWorld,normal_matrix=None,edge_profile=None):
    """Compute from rest points; reject an incomplete footprint atomically.

    target is a world-space MFnMesh. Returns fresh world-space MPoints.
    """
    if scale<=0:raise ValueError('Scale must be positive.')
    coords,height=coordinates(points,axis,frame)
    n=om.MVector(normal).normal()
    if n.length()<.5:raise ValueError('Surface normal is undefined.')
    ref=om.MVector(0,0,1) if abs(n.z)<.9 else om.MVector(0,1,0)
    u=(om.MVector(tangent)-n*(om.MVector(tangent)*n)).normal() if tangent is not None else (n^ref).normal()
    if u.length()<.5:u=(n^ref).normal()
    v=n^u;origin=om.MVector(anchor)
    extent=max(max(abs(a),abs(b),h) for a,h,b in coords)*scale
    reach=max(extent*4,1.)
    accelerator=target.autoUniformGridParams();cache={};result=[];clearances=[]
    for index,(a,h,b) in enumerate(coords):
        key=(a,b)
        if key not in cache:
            base=origin+u*(a*scale)+v*(b*scale)
            # Search from the local footprint, rather than from far outside:
            # a separate front layer must not steal the projection.
            hit=target.closestIntersection(om.MFloatPoint(base),om.MFloatVector(n),
                space,reach,True,accelParams=accelerator)
            if hit is None:raise ValueError('The floater footprint does not fit this surface.')
            delta=(om.MVector(hit[0])-base)*n
            hit_normal=target.getPolygonNormal(hit[2],space)
            if normal_matrix is not None:hit_normal=(hit_normal*normal_matrix).normal()
            if hit_normal*n<=.05:raise ValueError('Projection reaches another side of the surface.')
            cache[key]=delta
        t=max(0.,min(1.,h/height))
        displacement=cache[key]
        if edge_profile is not None and edge_profile['parameters'][index] is not None:
            t=edge_profile['parameters'][index]
        weight=ramp(t) if ramp else 1-t*t*(3-2*t)
        if not 0<=weight<=1:raise ValueError('Gradient influence must be between zero and one.')
        p=origin+u*(a*scale)+v*(b*scale)+n*(offset+h*scale+displacement*weight)
        result.append(om.MPoint(p))
        clearances.append(h*scale+displacement*weight-cache[key])
    # One rigid normal translation docks the lowest point. Unlike clamping
    # individual vertices this preserves the chosen deformation and thickness.
    correction=min(clearances)
    return [point-n*correction for point in result]
