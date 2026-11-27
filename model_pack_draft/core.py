"""Geometry and undoable scene operations. All API distances are centimeters."""

from contextlib import contextmanager
from dataclasses import dataclass
import math
import uuid

from maya import cmds
from maya.api import OpenMaya as om


EPSILON = 1e-6


def path_for(name):
    selection = om.MSelectionList()
    selection.add(name)
    return selection.getDagPath(0)


def ui_distance(centimeters):
    return om.MDistance(centimeters, om.MDistance.kCentimeters).asUnits(om.MDistance.uiUnit())


def internal_distance(value):
    return om.MDistance(value, om.MDistance.uiUnit()).asCentimeters()


@dataclass
class Frame:
    origin: om.MPoint
    normal: om.MVector
    u: om.MVector
    v: om.MVector

    @classmethod
    def make(cls, origin, normal, right=(1, 0, 0), angle=0.0):
        normal = om.MVector(normal).normal()
        if normal.length() < EPSILON:
            raise ValueError("Surface normal has zero length.")
        right = om.MVector(right)
        u = right - normal * (right * normal)
        if u.length() < EPSILON:
            right = min((om.MVector(1, 0, 0), om.MVector(0, 1, 0), om.MVector(0, 0, 1)),
                        key=lambda axis: abs(axis * normal))
            u = right - normal * (right * normal)
        u.normalize()
        if angle:
            u = u.rotateBy(om.MQuaternion(math.radians(angle), normal))
        return cls(om.MPoint(origin), normal, u, (u ^ normal).normal())

    def point(self, x, y, z):
        return self.origin + self.u * x + self.normal * y + self.v * z

    def coordinates(self, point):
        delta = om.MPoint(point) - self.origin
        return delta * self.u, delta * self.v


@dataclass
class Footprint:
    frame: Frame
    shape: str
    x: float
    z: float
    segments: int = 32
    centered: bool = False

    def set_base(self, x, z, square=False, centered=False):
        """Update from the original click, including live box constraints."""
        if self.shape == 'box' and square:
            side = max(abs(x), abs(z))
            x, z = math.copysign(side, x), math.copysign(side, z)
        self.x, self.z = x, z
        self.centered = self.shape == 'box' and centered

    @property
    def valid(self):
        if self.shape == "cylinder":
            return math.hypot(self.x, self.z) > EPSILON
        return abs(self.x) > EPSILON and abs(self.z) > EPSILON

    def dimensions(self):
        if self.shape == "cylinder":
            radius = math.hypot(self.x, self.z)
            return radius * 2, radius * 2, 0.0, 0.0
        if self.centered:
            return abs(self.x) * 2, abs(self.z) * 2, 0.0, 0.0
        return abs(self.x), abs(self.z), self.x / 2, self.z / 2

    def mesh(self, depth):
        """Closed preview topology, outward winding for either depth sign."""
        if self.shape == "cylinder":
            radius = math.hypot(self.x, self.z)
            ring = [(radius * math.cos(i * 2 * math.pi / self.segments),
                     radius * math.sin(i * 2 * math.pi / self.segments))
                    for i in range(self.segments)]
        else:
            width, length, center_x, center_z = self.dimensions()
            lo_x, hi_x = center_x - width / 2, center_x + width / 2
            lo_z, hi_z = center_z - length / 2, center_z + length / 2
            ring = [(lo_x, lo_z), (hi_x, lo_z), (hi_x, hi_z), (lo_x, hi_z)]
        return prism_mesh(self.frame, ring, depth)


def prism_mesh(frame, ring, depth):
    """One shared contour for the displayed and committed closed prism."""
    count = len(ring)
    low, high = sorted((0.0, depth))
    points = [frame.point(x, y, z) for y in (low, high) for x, z in ring]
    faces = [list(range(count)), list(reversed(range(count, count * 2)))]
    faces += [[i, i + count, (i + 1) % count + count, (i + 1) % count]
              for i in range(count)]
    return points, [len(face) for face in faces], [i for face in faces for i in face]


@dataclass
class Hit:
    path: om.MDagPath
    point: om.MPoint
    normal: om.MVector
    distance: float
    face: int = -1


def surface_frame(hit, angle=0.0):
    """Orthogonal surface axes from the object, independent of face diagonals.

    A Boolean or a beveled outline can give a planar face an arbitrary long
    diagonal. Its length must not rotate a cylinder around the drawing origin.
    Project local X onto the surface; use local Y/Z on faces nearly normal to X.
    """
    matrix=hit.path.inclusiveMatrix();normal=om.MVector(hit.normal).normal()
    axes=[(om.MVector(*axis)*matrix).normal() for axis in ((1,0,0),(0,1,0),(0,0,1))]
    projected=[axis-normal*(axis*normal) for axis in axes]
    tangent=projected[0]
    if tangent.length()<.1:tangent=max(projected[1:],key=lambda axis:axis.length())
    return Frame.make(hit.point, hit.normal, tangent, angle)


def raycast(origin, direction, candidates):
    """Pick the nearest visible mesh from an explicit viewport candidate list."""
    direction = om.MVector(direction).normal()
    nearest = None
    for path in candidates:
        if not path.isValid() or not path.isVisible():
            continue
        mesh = om.MFnMesh(path)
        if mesh.isIntermediateObject:
            continue
        result = mesh.closestIntersection(
            om.MFloatPoint(origin), om.MFloatVector(direction), om.MSpace.kWorld,
            1e10, False, accelParams=om.MFnMesh.autoUniformGridParams())
        if result and (nearest is None or result[1] < nearest.distance):
            nearest = Hit(om.MDagPath(path), om.MPoint(result[0]),
                          mesh.getPolygonNormal(result[2], om.MSpace.kWorld).normal(), result[1], result[2])
    return nearest


def intersect_plane(origin, direction, point, normal):
    denominator = direction * normal
    if abs(denominator) < 1e-5:
        return None
    distance = ((point - origin) * normal) / denominator
    if distance < 0:
        return None
    return origin + direction * distance


def normal_drag_axis(view, anchor, normal, units_per_pixel):
    """Freeze the outward surface normal's screen direction for one gesture.

    Maya draggerContext and M3dView.worldToView both use bottom-origin screen
    coordinates. Project at the footprint center, not the second click, so
    clicking elsewhere cannot reverse Add/Cut. Near a face-on normal there is
    no usable 2D direction; use a predictable upward Add gesture in that case.
    """
    length = max(units_per_pixel, EPSILON) * 64.0
    base = view.worldToView(om.MPoint(anchor))
    tip = view.worldToView(om.MPoint(anchor) + om.MVector(normal) * length)
    dx, dy = tip[0] - base[0], tip[1] - base[1]
    pixels = math.hypot(dx, dy)
    if not base[2] or not tip[2] or pixels < 8.0:
        return (0.0, 1.0), units_per_pixel, True
    return (dx / pixels, dy / pixels), length / pixels, False


@contextmanager
def transaction(label):
    if not cmds.undoInfo(query=True, state=True):
        raise RuntimeError("Enable Undo in Maya Preferences before using Fast Draft.")
    token = "Fast Draft {} [{}]".format(label, uuid.uuid4().hex[:8])
    cmds.undoInfo(openChunk=True, chunkName=token)
    try:
        yield
    except Exception:
        cmds.undoInfo(closeChunk=True)
        # A failed first command must never undo the user's previous operation.
        if cmds.undoInfo(query=True, undoName=True) == token:
            cmds.undo()
        raise
    else:
        cmds.undoInfo(closeChunk=True)


def create_primitive(footprint, depth, overlap=0.0, editable=False):
    if not footprint.valid or abs(depth) < EPSILON:
        raise ValueError("Draw a nonzero footprint and depth.")
    if getattr(footprint, 'from_curve', False):
        from .curve_profile import create
        return create(footprint, depth, overlap, editable)
    width, length, center_x, center_z = footprint.dimensions()
    bottom, top = sorted((0.0, depth))
    top += overlap
    if footprint.shape == "box":
        node = cmds.polyCube(name="fdBox#", width=ui_distance(width),
                             height=ui_distance(top - bottom), depth=ui_distance(length),
                             constructionHistory=False)[0]
    elif footprint.shape == "cylinder":
        created = cmds.polyCylinder(name="fdCylinder#", radius=ui_distance(width / 2),
                                 height=ui_distance(top - bottom),
                                 subdivisionsAxis=footprint.segments,
                                 subdivisionsHeight=1, subdivisionsCaps=0,
                                 constructionHistory=editable)
        node = created[0]
        if editable:
            from .cylinder_caps import attach
            attach(node, created[1])
    else:
        raise ValueError("Unknown primitive: " + footprint.shape)
    frame = footprint.frame
    center = frame.point(center_x, (bottom + top) / 2, center_z)
    matrix = list(frame.u) + [0.0] + list(frame.normal) + [0.0] + list(frame.v) + [0.0]
    # xform's matrix translation uses Maya internal centimeters even when the
    # current scene linear unit differs; primitive size flags use UI units.
    matrix += [center.x, center.y, center.z, 1.0]
    cmds.xform(node, worldSpace=True, matrix=matrix)
    if footprint.shape == 'cylinder' and not editable:
        from .cylinder_caps import soften_walls
        soften_walls(node, footprint)
    return cmds.ls(node, long=True)[0]


def commit(footprint, depth, target=None, keep_history=True):
    """Positive depth adds a separate primitive; negative depth subtracts a copy.

    Open curve profiles extrude a ribbon for either depth sign.
    Original mesh data and history are preserved. A successful cut hides only
    the picked original shape; Undo restores visibility and removes the result.
    """
    if not footprint.valid or abs(depth) < EPSILON:
        raise ValueError("Draw a nonzero footprint and depth.")
    surface=not getattr(footprint,'closed',True)
    shape = None
    if depth < 0 and not surface:
        if not target:
            raise ValueError("Subtraction requires a polygon mesh target.")
        path = path_for(target)
        if not path.node().hasFn(om.MFn.kMesh):
            raise ValueError("Subtraction target must be a polygon mesh shape.")
        shape = path.fullPathName()
        if path.isInstanced():
            raise ValueError("Make the target mesh unique before subtracting from an instance.")
        if not cmds.getAttr(shape + ".visibility", settable=True):
            raise ValueError("Target shape visibility is locked or connected. Unlock it first.")
    with transaction('Extrude Curve Surface' if surface else "Cut" if depth < 0 else "Add"):
        if depth > 0 or surface:
            result = create_primitive(footprint, depth, editable=footprint.shape == 'cylinder')
        else:
            width, length, _, _ = footprint.dimensions()
            # Extend above the tangent plane to avoid coplanar boolean faces.
            overlap = max(min(width, length, abs(depth)) * 0.001, 1e-5)
            cutter = create_primitive(footprint, depth, overlap)
            parent = cmds.listRelatives(shape, parent=True, fullPath=True)[0]
            original_shapes = cmds.listRelatives(parent, shapes=True, fullPath=True) or []
            picked_index = original_shapes.index(shape)
            snapshot = cmds.duplicate(parent, returnRootsOnly=True, name="fdSource#")[0]
            snapshot_shapes = cmds.listRelatives(snapshot, shapes=True, fullPath=True) or []
            if len(original_shapes) != len(snapshot_shapes):
                raise RuntimeError("Could not isolate the picked mesh in a target snapshot.")
            unwanted = [node for index, node in enumerate(snapshot_shapes) if index != picked_index]
            unwanted += cmds.listRelatives(snapshot, children=True, type="transform", fullPath=True) or []
            if unwanted:
                cmds.delete(unwanted)
            cmds.delete(snapshot, constructionHistory=True)
            # Carve tests cap planarity in world space. Its default 1e-6 can
            # split even a rigid cylinder cap at translated float coordinates.
            origin=footprint.frame.origin
            coordinate=max(abs(origin.x),abs(origin.y),abs(origin.z))+max(width,length,abs(depth))
            planar_tolerance=max(1e-6,coordinate*3e-7)
            result = cmds.polyCBoolOp(snapshot, cutter, operation=2,
                                     name="fdCut#", classification=1,planarTolerance=planar_tolerance)[0]
            result_shape = cmds.listRelatives(result, shapes=True, noIntermediate=True, fullPath=True)[0]
            if cmds.polyEvaluate(result_shape, face=True) == 0:
                raise RuntimeError("Boolean produced an empty mesh. Reduce depth or check target topology.")
            from .boolean_cleanup import clean
            clean(result,shape)
            if getattr(footprint, 'from_curve', False):
                from .curve_profile import soften_walls
                soften_walls(result,footprint,depth,history=True)
            elif footprint.shape == 'cylinder':
                from .cylinder_caps import soften_walls
                soften_walls(result, footprint, depth, history=True)
            if not keep_history:
                cmds.delete(result, constructionHistory=True)
            cmds.addAttr(result, longName="fdOriginal", attributeType="message")
            cmds.connectAttr(shape + ".message", result + ".fdOriginal")
            from .cuts import record
            record(result, footprint, depth)
            cmds.setAttr(shape + ".visibility", False)
        from .patterns import mark_created
        mark_created(result)
        cmds.select(result, replace=True)
        return cmds.ls(result, long=True)[0]
