"""Portable cut templates, including recovery of legacy Fast Draft operands."""

from dataclasses import dataclass
import json
import math

from maya import cmds
from maya.api import OpenMaya as om
from . import core


@dataclass
class CutTemplate:
    shape: str
    width: float
    length: float
    depth: float
    segments: int
    u: om.MVector
    normal: om.MVector

    @classmethod
    def from_vectors(cls, shape, width_axis, length_axis, depth_axis, segments):
        if shape not in ("box", "cylinder"):
            raise ValueError("Unsupported cut primitive.")
        axes = [om.MVector(axis) for axis in (width_axis, length_axis, depth_axis)]
        sizes = [axis.length() for axis in axes]
        if any(not math.isfinite(value) or value <= core.EPSILON for value in sizes):
            raise ValueError("Cut dimensions must be finite and nonzero.")
        units = [axis.normal() for axis in axes]
        if any(abs(units[i] * units[j]) > 1e-4 for i, j in ((0, 1), (0, 2), (1, 2))):
            raise ValueError("The cut is skewed by object scaling. Use an unskewed cut result.")
        if shape == "cylinder" and abs(sizes[0] - sizes[1]) > max(sizes[:2]) * 1e-4:
            raise ValueError("The scaled cut is elliptical; only circular cylinders can be copied.")
        if not 3 <= int(segments) <= 256:
            raise ValueError("Invalid cylinder segment count.")
        return cls(shape, *sizes, int(segments), units[0], units[2])

    def at(self, hit, angle=0.0):
        rotation = om.MQuaternion(self.normal, hit.normal)
        frame = core.Frame.make(hit.point, hit.normal, self.u.rotateBy(rotation), angle)
        if self.shape == "cylinder":
            return core.Footprint(frame, self.shape, self.width / 2, 0, self.segments)
        # A stamp is centered at the pointer, independent of the original
        # rectangle's signed corner-to-corner drawing direction.
        frame.origin = frame.point(-self.width / 2, 0, -self.length / 2)
        return core.Footprint(frame, self.shape, self.width, self.length, self.segments)


@dataclass
class ProfileTemplate:
    shape: str
    segments: int
    depth: float
    frame: core.Frame
    ring: list

    def at(self,hit,angle=0.):
        from .curve_profile import Profile
        rotation=om.MQuaternion(self.frame.normal,hit.normal)
        frame=core.Frame.make(hit.point,hit.normal,self.frame.u.rotateBy(rotation),angle)
        return Profile(frame,list(self.ring),self.shape,self.segments)


def record(result, footprint, depth):
    if getattr(footprint,'primitive',None) is not None:footprint=footprint.primitive
    inverse = core.path_for(result).inclusiveMatrixInverse()
    width, length, _, _ = footprint.dimensions()
    data = dict(schema=1, shape=footprint.shape, segments=footprint.segments,
                width_axis=list((footprint.frame.u * width) * inverse),
                length_axis=list((footprint.frame.v * length) * inverse),
                depth_axis=list((footprint.frame.normal * abs(depth)) * inverse))
    if getattr(footprint,'from_curve',False):
        data.update(schema=2,ring=[list((footprint.frame.u*x+footprint.frame.v*z)*inverse)
                                   for x,z in footprint.ring])
    if not cmds.attributeQuery("fdCutSpec", node=result, exists=True):
        cmds.addAttr(result, longName="fdCutSpec", dataType="string")
    cmds.setAttr(result + ".fdCutSpec", json.dumps(data), type="string")


def from_node(node):
    path = core.path_for(node)
    if path.node().hasFn(om.MFn.kMesh):
        path.pop()
    node = path.fullPathName()
    if cmds.attributeQuery("fdCutSpec", node=node, exists=True):
        try:
            data = json.loads(cmds.getAttr(node + ".fdCutSpec"))
            if data["schema"] == 2:
                matrix=path.inclusiveMatrix()
                offsets=[om.MVector(p)*matrix for p in data['ring']]
                axis=om.MVector(data['depth_axis'])*matrix
                frame=core.Frame.make(om.MPoint(),axis,offsets[1]-offsets[0])
                ring=[frame.coordinates(om.MPoint(p)) for p in offsets]
                from .curve_profile import _validate
                ring=_validate(ring,max(max(p.length() for p in offsets)*1e-7,1e-7))
                return ProfileTemplate(data['shape'],data['segments'],axis.length(),frame,ring)
            if data["schema"] != 1:
                raise ValueError("Unknown cut template version.")
            matrix = path.inclusiveMatrix()
            return CutTemplate.from_vectors(data["shape"],
                om.MVector(data["width_axis"]) * matrix,
                om.MVector(data["length_axis"]) * matrix,
                om.MVector(data["depth_axis"]) * matrix, data["segments"])
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise ValueError("Saved cut parameters are incomplete or damaged.") from error
    return _from_legacy_history(node, path.inclusiveMatrix())


def from_selection():
    selected = cmds.ls(selection=True, long=True, objectsOnly=True) or []
    transforms = set()
    for item in selected:
        path = core.path_for(item)
        if path.node().hasFn(om.MFn.kMesh):
            path.pop()
        transforms.add(path.fullPathName())
    if len(transforms) != 1:
        raise ValueError("Select one completed Fast Draft cut result, then click Copy Cut.")
    return from_node(transforms.pop())


def _from_legacy_history(node, result_matrix):
    if not cmds.attributeQuery("fdOriginal", node=node, exists=True):
        raise ValueError("Select a Fast Draft cut result, rather than an added primitive.")
    history = cmds.ls(cmds.listHistory(node) or [], type="polyCBoolOp")
    if not history or cmds.getAttr(history[0] + ".operation") != 2:
        raise ValueError("This older cut has no Boolean history. Create a new cut to copy it.")
    sources = cmds.listConnections(history[0] + ".inputPoly[1]", source=True,
                                   destination=False, shapes=True) or []
    if not sources:
        raise ValueError("The old Boolean cutter is missing.")
    cutter = core.path_for(sources[0])
    if not cutter.node().hasFn(om.MFn.kMesh):
        cutter.extendToShape()
    mesh = om.MFnMesh(cutter)
    points = mesh.getPoints(om.MSpace.kObject)
    minimum = [min(point[i] for point in points) for i in range(3)]
    maximum = [max(point[i] for point in points) for i in range(3)]
    width, height, length = [maximum[i] - minimum[i] for i in range(3)]
    tolerance = max(width, height, length) * 1e-5
    if len(points) == 8 and mesh.numPolygons == 6:
        if any(all(abs(point[i] - bound) > tolerance for bound in (minimum[i], maximum[i]))
               for point in points for i in range(3)):
            raise ValueError("The old box cutter was edited and is no longer rectangular.")
        shape, segments = "box", 32
    else:
        center_x = sum(point.x for point in points) / len(points)
        center_z = sum(point.z for point in points) / len(points)
        radii = [math.hypot(point.x - center_x, point.z - center_z) for point in points]
        radius = max(radii)
        if any(abs(point.y - minimum[1]) > tolerance and abs(point.y - maximum[1]) > tolerance
               for point in points) or any(tolerance < r < radius - tolerance for r in radii):
            raise ValueError("The old cutter was edited and is no longer a box or circular cylinder.")
        segments = sum(abs(point.y - minimum[1]) < tolerance and r > tolerance
                       for point, r in zip(points, radii))
        shape, width, length = "cylinder", 2 * radius, 2 * radius
    # Older Fast Draft cutters included a small outward overlap. Recover the
    # user-entered inward depth, avoiding a slightly deeper copy each time.
    depth = height / 1.001
    if depth > min(width, length):
        depth = height - min(width, length) * 0.001
    if min(width, length, depth) * 0.001 < 1e-5:
        depth = height - 1e-5
    matrix = cutter.inclusiveMatrix() * result_matrix
    return CutTemplate.from_vectors(shape,
        om.MVector(width, 0, 0) * matrix, om.MVector(0, 0, length) * matrix,
        om.MVector(0, depth, 0) * matrix, segments)
