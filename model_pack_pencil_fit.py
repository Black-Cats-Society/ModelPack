"""Native cubic fitting with bounded-error reduction for editable pencil strokes."""
import math
from maya import cmds
from maya.api import OpenMaya as om
from model_pack_draft.core import ui_distance


def segment_distance(point,a,b):
    vector=b-a;squared=vector*vector
    t=max(0.,min(1.,((point-a)*vector)/squared)) if squared>1e-24 else 0.
    return (point-(a+vector*t)).length()


def simplify(points,tolerance):
    """Iterative RDP, retaining both ends and the original drawing order."""
    if len(points)<3:return list(points)
    keep={0,len(points)-1};pending=[(0,len(points)-1)]
    while pending:
        first,last=pending.pop()
        distance,index=max(((segment_distance(points[i],points[first],points[last]),i)
                            for i in range(first+1,last)),default=(0,None))
        if distance>tolerance:
            keep.add(index);pending.extend(((first,index),(index,last)))
    return [points[i] for i in sorted(keep)]


def curve_fn(name):
    dag=om.MSelectionList().add(name).getDagPath(0)
    if dag.node().hasFn(om.MFn.kTransform):dag.extendToShape()
    return om.MFnNurbsCurve(dag)


def closed_samples(points,tolerance):
    lengths=[(b-a).length() for a,b in zip(points,points[1:])];total=sum(lengths)
    count=max(8,math.ceil(total/(tolerance*.5)))
    if count>24000:raise ValueError('Штрих слишком длинный для такой точности. Увеличьте Simplify.')
    result=[];segment=0;start=0.
    for index in range(count):
        distance=total*index/count
        while segment<len(lengths)-1 and start+lengths[segment]<distance:
            start+=lengths[segment];segment+=1
        a,b=points[segment:segment+2]
        result.append(a+(b-a)*((distance-start)/lengths[segment]))
    return result


def within_error(name,points,polyline,tolerance):
    """Check both directions, rejecting fits which miss a turn or grow a loop."""
    fn=curve_fn(name);closest_tolerance=max(tolerance*.01,1e-10)
    for p in points:
        nearest=fn.closestPoint(p,space=om.MSpace.kWorld,tolerance=closest_tolerance)[0]
        if (nearest-p).length()>tolerance:return False
    low,high=fn.knotDomain
    for i in range(129):
        p=fn.getPointAtParam(low+(high-low)*i/128,om.MSpace.kWorld)
        if min(segment_distance(p,a,b) for a,b in zip(polyline,polyline[1:]))>tolerance:return False
    return True


def create(points,tolerance,closed=False):
    """One native Undo command; no source stroke/history survives in the scene."""
    if not math.isfinite(tolerance) or tolerance<=0:raise ValueError('Допуск упрощения должен быть положительным.')
    cleaned=[]
    for value in points:
        point=om.MPoint(value)
        if not all(math.isfinite(v) for v in (point.x,point.y,point.z)):raise ValueError('У штриха неверные координаты.')
        if not cleaned or (point-cleaned[-1]).length()>max(tolerance*1e-5,1e-12):cleaned.append(point)
    if len(cleaned)<2:raise ValueError('Нарисуйте линию, удерживая левую кнопку мыши.')
    if closed:
        if (cleaned[0]-cleaned[-1]).length()>tolerance*1e-5:cleaned.append(om.MPoint(cleaned[0]))
        else:cleaned[-1]=om.MPoint(cleaned[0])
    if max((p-cleaned[0]).length() for p in cleaned)<=tolerance*.1:raise ValueError('Штрих слишком короткий.')
    straight=not closed and max(segment_distance(p,cleaned[0],cleaned[-1]) for p in cleaned)<=tolerance
    if not cmds.undoInfo(q=True,state=True):raise ValueError('Включите Undo в Maya перед рисованием кривой.')
    cmds.undoInfo(openChunk=True,chunkName='Pencil Curve')
    created=[]
    try:
        values=lambda sequence:[tuple(ui_distance(v) for v in (p.x,p.y,p.z)) for p in sequence]
        if straight:
            result=cmds.curve(name='pencilCurve#',degree=1,point=values((cleaned[0],cleaned[-1])));created.append(result)
        else:
            if closed:
                # Start periodic, avoiding closeCurve's extra seam/blend arc.
                periodic=closed_samples(cleaned,tolerance);periodic+=periodic[:3]
                source=cmds.curve(name='pencilStroke#',degree=3,periodic=True,point=values(periodic),knot=list(range(len(periodic)+2)))
                created.append(source);result=source
            else:
                # Native fitting treats the input CVs as samples. Keep samples
                # along straight runs too, so a sharp turn cannot become a wide arc.
                source=cmds.curve(name='pencilStroke#',degree=1,point=values(cleaned));created.append(source)
                result=cmds.fitBspline(source,constructionHistory=False,tolerance=ui_distance(tolerance*.65),name='pencilCurve#')[0]
                created.append(result)
            # Native fitBspline is adaptive; try fewer uniform cubic spans and
            # retain a candidate only after checking its actual drawn shape.
            polyline=simplify(cleaned,tolerance*.12)
            if not closed and not within_error(result,cleaned,polyline,tolerance*.9):
                for fraction in (.15,.03):
                    cmds.delete(result)
                    result=cmds.fitBspline(source,constructionHistory=False,tolerance=ui_distance(tolerance*fraction),name='pencilCurve#')[0]
                    created.append(result)
                    if within_error(result,cleaned,polyline,tolerance*.9):break
                else:
                    # Sparse sharp clicks have insufficient samples for a
                    # smooth fit. Their exact polyline retains the intended turn.
                    cmds.delete(result);result=source
            low,high=1,max(0,curve_fn(result).numCVs-4)
            for _ in range(16):
                if low>high:break
                spans=(low+high)//2
                candidate=cmds.rebuildCurve(result,constructionHistory=False,replaceOriginal=False,
                    rebuildType=0,spans=spans,degree=3,keepRange=0,keepControlPoints=False,
                    keepEndPoints=True,keepTangents=False,fitRebuild=True,tolerance=ui_distance(tolerance*.25),
                    name='pencilCandidate#')[0]
                created.append(candidate)
                if (curve_fn(candidate).numCVs<curve_fn(result).numCVs and
                        (not closed or curve_fn(candidate).form!=om.MFnNurbsCurve.kOpen) and
                        within_error(candidate,cleaned,polyline,tolerance*.85)):
                    cmds.delete(result);result=candidate;high=spans-1
                else:
                    cmds.delete(candidate);low=spans+1
            if source!=result and cmds.objExists(source):cmds.delete(source)
            result=cmds.rename(result,'pencilCurve#')
        cmds.xform(result,centerPivots=True)
        cmds.selectMode(object=True);cmds.select(result,replace=True)
    except Exception:
        cmds.undoInfo(closeChunk=True)
        if created and cmds.undoInfo(q=True,undoName=True)=='Pencil Curve':cmds.undo()
        raise
    else:cmds.undoInfo(closeChunk=True)
    return (cmds.ls(result,long=True) or [result])[0]
