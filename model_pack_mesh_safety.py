"""Owned, serializable last-good geometry for curve-driven mesh evaluators."""
import math
from maya.api import OpenMaya as om

NAME='lastValidMesh'


def cache_attribute():
    typed=om.MFnTypedAttribute()
    attr=typed.create(NAME,'lastMesh',om.MFnData.kMesh)
    typed.hidden=True;typed.storable=True;typed.writable=True
    return attr


def register_cache(cls):
    cls.addAttribute(cache_attribute())


def ensure_cache_type(kind):
    node_class=om.MNodeClass(kind)
    try:missing=node_class.attribute(NAME).isNull()
    except RuntimeError:missing=True
    if missing:node_class.addExtensionAttribute(cache_attribute())


def direction_attribute():
    numeric=om.MFnNumericAttribute()
    attr=numeric.create('flipDirection','flip',om.MFnNumericData.kBoolean,False)
    numeric.keyable=True
    return attr


def ensure_direction_type(kind):
    node_class=om.MNodeClass(kind)
    try:missing=node_class.attribute('flipDirection').isNull()
    except RuntimeError:missing=True
    if missing:node_class.addExtensionAttribute(direction_attribute())


def is_flipped(node,data):
    fn=om.MFnDependencyNode(node)
    return data.inputValue(fn.attribute('flipDirection')).asBool() if fn.hasAttribute('flipDirection') else False


def owned_copy(source):
    result=om.MFnMeshData().create();om.MFnMesh().copy(source,result)
    return result


def validate(source):
    if source.isNull():raise ValueError('Model Pack: отсутствует сетка.')
    fn=om.MFnMesh(source)
    if fn.numVertices<3 or fn.numPolygons<1:raise ValueError('Model Pack: пустая сетка.')
    if any(not math.isfinite(v) for point in fn.getPoints() for v in tuple(point)[:3]):
        raise ValueError('Model Pack: некорректные координаты сетки.')
    counts,ids=fn.getVertices()
    if any(c<3 for c in counts) or any(i<0 or i>=fn.numVertices for i in ids):
        raise ValueError('Model Pack: некорректная топология сетки.')


def remember(node,data,source):
    validate(source)
    fn=om.MFnDependencyNode(node)
    if fn.hasAttribute(NAME):
        attr=fn.attribute(NAME)
        data.outputValue(attr).setMObject(owned_copy(source))
        data.setClean(om.MPlug(node,attr))
    return source


def recover(node,data):
    fn=om.MFnDependencyNode(node)
    if fn.hasAttribute(NAME):
        source=data.inputValue(fn.attribute(NAME)).asMesh()
        try:validate(source)
        except (ValueError,RuntimeError,TypeError):pass
        else:return owned_copy(source)
    return om.MFnMeshData().create()
