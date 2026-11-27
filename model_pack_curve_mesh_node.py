"""Curve Mesh dependency node and quad loft geometry."""
import json

from maya.api import OpenMaya as om


def maya_useNewAPI():
    pass


def sample_section(curve, count, closed=False, space=om.MSpace.kObject):
    length = curve.length()
    if length <= 1e-8:
        raise ValueError('A section has zero length.')
    start, end = curve.knotDomain
    result = [curve.getPointAtParam(start, space)]
    for index in range(1, count):
        distance = length * (index / count)
        try:
            parameter = curve.findParamFromLength(distance)
            if not start <= parameter <= end:
                raise RuntimeError('Invalid arc-length parameter.')
        except RuntimeError:
            # Some curve shapes cannot invert arc length reliably while a CV
            # is being dragged. Bisect against forward arc length instead.
            lo, hi = start, end
            for _ in range(32):
                mid = (lo + hi) * .5
                if curve.findLengthFromParam(mid) < distance:
                    lo = mid
                else:
                    hi = mid
            parameter = (lo + hi) * .5
        result.append(curve.getPointAtParam(parameter, space))
    if not closed:
        # The endpoint is known exactly. Avoid asking Maya to invert a length
        # that can exceed its internal total by floating-point rounding.
        result.append(curve.getPointAtParam(end, space))
    return result


def loft_mesh(curves, across, between, settings, smooth, influences=None):
    if len(curves) < 2 or not 3 <= across <= 128 or not 1 <= between <= 128:
        raise ValueError('Invalid Loft dimensions.')
    if (len(curves) - 1) * across * between > 250000:
        raise ValueError('Loft exceeds 250,000 quads.')
    profiles = []
    closed = curves[0].form != om.MFnNurbsCurve.kOpen
    count = across if closed else across + 1
    for index, curve in enumerate(curves):
        if (curve.form != om.MFnNurbsCurve.kOpen) != closed:
            raise ValueError('Use all open or all closed sections.')
        samples = sample_section(curve, across, closed)
        reverse, shift = settings[index]
        if reverse:
            samples = samples[:1] + samples[:0:-1] if closed else samples[::-1]
        if closed:
            shift %= count
            samples = samples[shift:] + samples[:shift]
        profiles.append(samples)

    points = []
    for index, (left, right) in enumerate(zip(profiles, profiles[1:])):
        before = profiles[max(0, index - 1)]
        after = profiles[min(len(profiles) - 1, index + 2)]
        for step in range(between):
            t = step / between
            for a, b, c, d in zip(before, left, right, after):
                if smooth:
                    m0, m1 = (c - a) * .5, (d - b) * .5
                    if index == 0:
                        m0 = c - b
                    if index == len(profiles) - 2:
                        m1 = c - b
                    points.append(b + (c-b)*(3*t*t-2*t*t*t)
                                  + m0*(t*t*t-2*t*t+t) + m1*(t*t*t-t*t))
                else:
                    points.append(b + (c-b)*t)
    points.extend(profiles[-1])
    if influences:
        from recurve_controls import falloff_weight
        # Keep progression through the loft fixed. Weights shape the
        # transverse coordinates rather than sliding vertices down each rail.
        centers = [sum((point - om.MPoint() for point in profile), om.MVector()) / count
                   for profile in profiles]
        axes = [(right - left).normal() for left, right in zip(centers, centers[1:])]
        for column in range(count):
            stations = [0.]
            for left, right in zip(profiles, profiles[1:]):
                stations.append(stations[-1] + (right[column] - left[column]).length())
            for row in range((len(profiles) - 1) * between + 1):
                segment = min(row // between, len(profiles) - 2)
                t = (row - segment * between) / between
                location = stations[segment] + (stations[segment+1] - stations[segment]) * t
                if row % between == 0:
                    section = row // between
                    if influences[section].get('align', True):
                        points[row*count+column] = profiles[section][column]
                        continue
                total = om.MVector()
                weight_sum = 0.
                for index, profile in enumerate(profiles):
                    config = influences[index]
                    if config.get('mode', 2) == 1:
                        distance = abs(row - index * between)
                        radius = config.get('edges', 4) + 1
                    else:
                        distance = abs(location - stations[index])
                        if config.get('mode', 2) == 0:
                            radius = config.get('distance', 1.)
                        else:
                            neighbor = min(index+1, len(profiles)-1) if location >= stations[index] else max(0, index-1)
                            radius = abs(stations[neighbor] - stations[index]) * config.get('percent', 100) / 100.
                    if distance < 1e-10:
                        weight = 1.
                    elif radius > 0 and distance < radius:
                        weight = falloff_weight(distance/radius, config.get('falloff', ((0.,1.),(1.,0.))), config.get('profile', 0))
                    else:
                        continue
                    total += (profile[column] - om.MPoint()) * weight
                    weight_sum += weight
                if weight_sum > 1e-8:
                    base = points[row*count+column]
                    weighted = om.MPoint(total / weight_sum)
                    axis = axes[segment]
                    offset = weighted - base
                    points[row*count+column] = base + offset - axis * (offset * axis)
    faces = []
    for row in range((len(profiles) - 1) * between):
        for column in range(across):
            next_column = (column + 1) % count
            faces.extend((row*count+column, row*count+next_column,
                          (row+1)*count+next_column, (row+1)*count+column))
    result = om.MFnMeshData().create()
    om.MFnMesh().create(points, [4]*(len(faces)//4), faces, parent=result)
    return result


def boundary_mesh(curves, across, between, settings):
    """Coons patch with a shared vertex at each of the four corners."""
    if len(curves) != 4 or any(curve.form != curve.kOpen for curve in curves):
        raise ValueError('Boundary needs four open curves.')
    if not 3 <= across <= 128 or not 1 <= between <= 128:
        raise ValueError('Invalid Boundary dimensions.')

    def sample(curve, count, reverse):
        points = sample_section(curve, count)
        return points[::-1] if reverse else points

    sides = [sample(curve, across if i % 2 == 0 else between, settings[i][0])
             for i, curve in enumerate(curves)]
    # CV edits can separate two ends that originally shared a corner.
    # Meet halfway and taper the endpoint correction along each side.
    corners = [side[-1] + (following[0] - side[-1]) * .5
               for side, following in zip(sides, sides[1:] + sides[:1])]
    for i, side in enumerate(sides):
        start = corners[i - 1] - side[0]
        end = corners[i] - side[-1]
        last = len(side) - 1
        for j, point in enumerate(side):
            t = j / last
            side[j] = point + start * (1. - t) + end * t
    bottom, right, top, left = sides
    top = top[::-1]
    left = left[::-1]
    origin = corners[3]
    b0, b1, t1, t0 = (corner - origin for corner in (corners[3], *corners[:3]))
    points = []
    for j in range(between + 1):
        v = j / between
        for i in range(across + 1):
            u = i / across
            horizontal = (bottom[i] - origin)*(1-v) + (top[i] - origin)*v
            vertical = (left[j] - origin)*(1-u) + (right[j] - origin)*u
            bilinear = b0*((1-u)*(1-v)) + b1*(u*(1-v)) + t0*((1-u)*v) + t1*(u*v)
            points.append(origin + horizontal + vertical - bilinear)
    stride = across + 1
    faces = []
    for j in range(between):
        for i in range(across):
            first = j * stride + i
            faces.extend((first, first+1, first+stride+1, first+stride))
    result = om.MFnMeshData().create()
    om.MFnMesh().create(points, [4]*(len(faces)//4), faces, parent=result)
    return result


class CurveMeshNode(om.MPxNode):
    name = 'modelPackCurveMesh'
    node_id = om.MTypeId(0x0013B2FA)

    @staticmethod
    def creator():
        return CurveMeshNode()

    @staticmethod
    def initialize():
        cls = CurveMeshNode
        typed = om.MFnTypedAttribute()
        cls.sections = typed.create('sections', 'sec', om.MFnData.kNurbsCurve)
        typed.array = True
        typed.storable = False
        cls.addAttribute(cls.sections)
        cls.options = typed.create('options', 'opt', om.MFnData.kString)
        cls.addAttribute(cls.options)
        cls.output = typed.create('outputMesh', 'out', om.MFnData.kMesh)
        typed.writable = False
        typed.storable = False
        cls.addAttribute(cls.output)
        cls.attributeAffects(cls.sections, cls.output)
        cls.attributeAffects(cls.options, cls.output)

    def compute(self, plug, data):
        cls = type(self)
        if plug != cls.output:
            return
        try:
            options = json.loads(data.inputValue(cls.options).asString())
            sections = data.inputArrayValue(cls.sections)
            curves = []
            for index in range(len(options['sections'])):
                sections.jumpToLogicalElement(index)
                curves.append(om.MFnNurbsCurve(sections.inputValue().asNurbsCurveTransformed()))
            if options.get('mode') == 'boundary':
                mesh = boundary_mesh(curves, options['across'], options['between'], options['sections'])
            else:
                mesh = loft_mesh(curves, options['across'], options['between'],
                                 options['sections'], options['smooth'], options.get('influences'))
        except (RuntimeError, ValueError, TypeError, KeyError):
            mesh = om.MFnMeshData().create()
        data.outputValue(cls.output).setMObject(mesh)
        data.setClean(plug)


def initializePlugin(plugin):
    cls = CurveMeshNode
    om.MFnPlugin(plugin, 'Model Pack', '1.0', 'Any').registerNode(
        cls.name, cls.node_id, cls.creator, cls.initialize)


def uninitializePlugin(plugin):
    om.MFnPlugin(plugin).deregisterNode(CurveMeshNode.node_id)


