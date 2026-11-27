"""Maya Python dependency node for saved Bevel width fields and profiles.

Two native bevel nodes provide flat/round topology. This node deforms that
topology; changing segments invalidates the template cache automatically.
"""
import json
import math
import sys
from pathlib import Path
_root = str(Path(globals().get("__file__",sys._getframe().f_code.co_filename)).resolve().parents[1])
if _root not in sys.path:
    sys.path.insert(0,_root)
from maya.api import OpenMaya as om
from model_pack_bevel.geometry import TemplateGeometry, clean_sharp_template
from model_pack_bevel.graph import EdgeGraph
from model_pack_bevel.profiles import Curve
from model_pack_bevel.surface import append_rim_caps

NODE_NAME = "modelPackGestureBevel"
NODE_ID = om.MTypeId(0x0013B2F1)


def maya_useNewAPI():
    pass


class GestureBevelNode(om.MPxNode):
    def __init__(self):
        super().__init__()
        self._key = None
        self._template = None
        self._limits = None

    @staticmethod
    def creator():
        return GestureBevelNode()

    @staticmethod
    def initialize():
        cls = GestureBevelNode
        cls.inputs = []
        typed = om.MFnTypedAttribute()
        for name, short in (("sourceMesh", "src"), ("flatMesh", "flt"), ("roundMesh", "rnd")):
            attr = typed.create(name, short, om.MFnData.kMesh)
            typed.storable = False
            typed.hidden = True
            cls.addAttribute(attr)
            setattr(cls, name, attr)
            cls.inputs.append(attr)
        cls.outputMesh = typed.create("outputMesh", "out", om.MFnData.kMesh)
        typed.storable = False
        typed.writable = False
        cls.addAttribute(cls.outputMesh)
        numeric = om.MFnNumericAttribute()
        for name, short, typ, value in (("width", "w", om.MFnNumericData.kDouble, 0.0),
                                        ("shape", "shp", om.MFnNumericData.kDouble, 0.0),
                                        ("referenceWidth", "ref", om.MFnNumericData.kDouble, .001),
                                        ("segments", "seg", om.MFnNumericData.kInt, 1)):
            attr = numeric.create(name, short, typ, value)
            numeric.keyable = name in ("width", "shape", "segments")
            if name in ("width", "referenceWidth"):
                numeric.setMin(0)
            elif name == "segments":
                numeric.setMin(1)
                numeric.setMax(64)
            else:
                numeric.setMin(-1)
                numeric.setMax(1)
            cls.addAttribute(attr)
            setattr(cls, name, attr)
            cls.inputs.append(attr)
        string_data = om.MFnStringData()
        for name, short, default in (("selectedEdges", "edges", "[]"),
                                      ("widthWeights", "weights", "{}"),
                                      ("widthLimits", "limits", "{}"),
                                      ("profileKind", "kind", "round"),
                                      ("profileCurve", "curve", "[[0,0,null],[0.5,0.5,null],[1,0,null]]"),
                                      ("falloffKind", "fallkind", "round"),
                                      ("falloffCurve", "fallcurve", "[[0,1,null],[1,0,null]]")):
            attr = typed.create(name, short, om.MFnData.kString, string_data.create(default))
            typed.storable = True
            typed.writable = True
            typed.hidden = False
            cls.addAttribute(attr)
            setattr(cls, name, attr)
            cls.inputs.append(attr)
        matrix = om.MFnMatrixAttribute()
        cls.meshMatrix = matrix.create("meshMatrix", "mat")
        matrix.hidden = True
        cls.addAttribute(cls.meshMatrix)
        cls.inputs.append(cls.meshMatrix)
        for attr in cls.inputs:
            cls.attributeAffects(attr, cls.outputMesh)

    def compute(self, plug, data):
        cls = type(self)
        if plug != cls.outputMesh:
            # Python API 2.0 uses None for plugs handled by the base node.
            return None
        source = data.inputValue(cls.sourceMesh).asMesh()
        if not json.loads(data.inputValue(cls.selectedEdges).asString()) and not source.isNull():
            output = om.MFnMeshData().create()
            om.MFnMesh().copy(source,output)
            data.outputValue(cls.outputMesh).setMObject(output)
            data.setClean(plug)
            return
        flat = data.inputValue(cls.flatMesh).asMesh()
        rounded = data.inputValue(cls.roundMesh).asMesh()
        if source.isNull() or flat.isNull() or rounded.isNull():
            return
        matrix = data.inputValue(cls.meshMatrix).asMatrix()
        fn = om.MFnDependencyNode(self.thisMObject())
        meta = json.loads(data.inputValue(fn.attribute("sessionMeta")).asString()) if fn.hasAttribute("sessionMeta") else {}
        sharp = meta.get("sharp", False)
        round_supports = meta.get("round_supports", 0) if not sharp else 0
        round_side_contacts = meta.get("round_side_contacts", False) and not sharp
        corner_grids = meta.get("corner_grids", False)
        preserve_open_rim = meta.get("preserve_open_rim", False)
        if not sharp and meta.get('junction_fill',False) and fn.hasAttribute('junctionFlat') and fn.hasAttribute('junctionRound'):
            first=data.inputValue(fn.attribute('junctionFlat')).asMesh();second=data.inputValue(fn.attribute('junctionRound')).asMesh()
            if not first.isNull() and not second.isNull():flat,rounded=first,second
        fn_source, fn_flat, fn_round = (om.MFnMesh(mesh) for mesh in (source, flat, rounded))
        key = (om.MObjectHandle(source).hashCode(),om.MObjectHandle(flat).hashCode(),
               om.MObjectHandle(rounded).hashCode(),fn_flat.numVertices,fn_round.numVertices,
               tuple(matrix),bool(meta.get('junction_fill',False)),sharp,
               bool(meta.get("clean_template")),round_supports,round_side_contacts,corner_grids,
               preserve_open_rim,data.inputValue(cls.segments).asInt())
        if self._template is None or key != self._key:
            points = fn_source.getPoints()
            selected = json.loads(data.inputValue(cls.selectedEdges).asString())
            reference = data.inputValue(cls.referenceWidth).asDouble()
            if sharp and meta.get("clean_template"):
                flat = clean_sharp_template(flat, matrix, reference)
                fn_flat = fn_round = om.MFnMesh(flat)
            flat_points, round_points = fn_flat.getPoints(), fn_round.getPoints()
            graph = EdgeGraph(points, ((e, fn_source.getEdgeVertices(e)) for e in selected))
            blends=json.loads(data.inputValue(fn.attribute('junctionBlends')).asString() or '{}') if not sharp and meta.get('junction_fill') and fn.hasAttribute('junctionBlends') else {}
            self._template = TemplateGeometry(points, flat_points, round_points, graph, reference, matrix,
                                              fn_flat.getVertices(),fn_flat.getTriangles(),blends,sharp=sharp,
                                              round_supports=round_supports,segments=key[-1],
                                              round_side_contacts=round_side_contacts,corner_grids=corner_grids,
                                              source_topology=fn_source.getVertices() if preserve_open_rim else None)
            self._template_mesh=om.MFnMeshData().create()
            cached = om.MFnMesh(om.MFnMesh().copy(flat,self._template_mesh))
            self._rim_cap_points = append_rim_caps(cached, fn_source, self._template.rim_caps)
            self._key = key
            self._limits = None
        weights = {int(k): v for k, v in json.loads(data.inputValue(cls.widthWeights).asString()).items()}
        limit_text = data.inputValue(cls.widthLimits).asString()
        if self._limits is None or getattr(self,"_limit_text",None) != limit_text:
            self._limits = {}
            for vertex,record in json.loads(limit_text).items():
                if isinstance(record,(int,float)):
                    limit = record
                else:
                    limit = sum(weight * .4 * min(math.dist(tuple(self._template.source[a])[:3],
                                        tuple(self._template.source[b])[:3])
                                        for b in record["rails"][str(a)])
                                for a,weight in record["blend"])
                self._limits[int(vertex)] = limit
            self._limit_text = limit_text
        curve = Curve(tuple(tuple(p) for p in json.loads(data.inputValue(cls.profileCurve).asString())), False)
        width = data.inputValue(cls.width).asDouble()
        spacing = meta.get("support_spacing")
        ratio = (meta.get("support_percent") or 0.) / 100.
        step = width * ratio if spacing is None else spacing
        if sharp:
            positions = self._template.evaluate_sharp(width, weights,
                meta.get("support_edges", 0) if step else 0, step, meta.get("keep_equal", True),
                fn_source.getVertices(), meta.get("preserve_rails", False), meta.get("clean_template", False),
                local_width_limits=meta.get("local_width_limits", False),
                corner_grids=corner_grids)
            self._template.reported_width = self._template.effective_width
        else:
            positions = self._template.evaluate(width + round_supports * step, data.inputValue(cls.shape).asDouble(), weights, self._limits,
                data.inputValue(cls.profileKind).asString(), curve, meta.get("keep_equal", True))
            self._template.reported_width = self._template.effective_width
            if round_supports and width > 1e-9:
                extent = self._template.effective_width
                if spacing is None:
                    fitted_width = extent / (1. + round_supports * ratio)
                    step = fitted_width * ratio
                else:
                    fitted_width = extent - round_supports * step
                self._template.reported_width = max(0., fitted_width)
                positions = self._template.fit_rounded_supports(positions, round_supports, step)
            if preserve_open_rim:
                positions = self._template.preserve_rounded_surface(positions)
                for point in self._rim_cap_points:
                    positions.append(point)
        output = om.MFnMeshData().create()
        result = om.MFnMesh()
        result.copy(self._template_mesh, output)
        result.setPoints(positions)
        self._template.apply_edge_smoothing(result, fn_source, multi=not sharp and key[-1]>1, sharp=sharp)
        self._welded_faces = None
        if sharp and meta.get("weld_contacts"):
            self._welded_faces = self._template.weld_sharp_contacts(result)
        data.outputValue(cls.outputMesh).setMObject(output)
        data.setClean(plug)

    def setDependentsDirty(self,plug,affected):
        cls = type(self)
        if om.MFnAttribute(plug.attribute()).name=="sessionMeta":
            affected.append(om.MPlug(self.thisMObject(),cls.outputMesh))
        if om.MFnAttribute(plug.attribute()).name in ('junctionFlat','junctionRound','junctionBlends'):
            self._template=None;self._key=None;affected.append(om.MPlug(self.thisMObject(),cls.outputMesh))
        if plug.attribute() in (cls.sourceMesh,cls.flatMesh,cls.roundMesh,
                                cls.meshMatrix,cls.selectedEdges,cls.referenceWidth):
            self._template = None
            self._key = None


def initializePlugin(plugin):
    om.MFnPlugin(plugin, "Model Pack", "1.0", "Any").registerNode(
        NODE_NAME, NODE_ID, GestureBevelNode.creator, GestureBevelNode.initialize)


def uninitializePlugin(plugin):
    om.MFnPlugin(plugin).deregisterNode(NODE_ID)
