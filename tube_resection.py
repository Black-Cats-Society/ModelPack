# -*- coding: utf-8 -*-
"""
tube_resection.py  —  Maya 2027  (Model Pack toolbox)

A single toolbox that groups the Model Pack tools:

  * Model Pack   : Combine, Separate, Extract/Detach Faces, Mirror, Mirror Dup,
                   interactive Clone Slide and Slide Bevel.
  * Hard Edges   : select edges whose polygon angle >= a threshold.
  * Resect Tube  : change the cross-section side count of a swept tube / hose
                   with clean grid topology, preserving shape and UV layout.

Resect Tube spec:
  * Section  : angular even -> clean regular N-gon (radius sampled from the
               original contour by angle, so ovality / varying radius survive).
  * UV       : preserve current layout, read per face-corner (several shells
               along the length are kept); seam stays in place.
  * Caps     : auto -> open end stays open, capped end rebuilt as an N-gon;
               a closed-loop tube is supported (no caps).

Usage:
    import tube_resection
    tube_resection.launch_toolbox()

Tool settings open below the main buttons.
For Resect Tube, select the length-wise seam
edge loop first, then click the button. A new mesh "<name>_resect" is created;
the original is left untouched.
"""

import os
import math
import importlib
import sys

import maya.api.OpenMaya as om
import maya.cmds as cmds
from slide_for_maya import ui as slide_ui
from slide_for_maya.core import _mesh_fn
from model_pack_bevel import ui as gesture_bevel_ui
from model_pack_bevel import unbevel as unbevel_ui
from model_pack_bevel import bevel_detection
from model_pack_selection import ui as selection_ui
from model_pack_linear_shift import ui as linear_shift_ui
import model_pack_hard_edges as hard_edges
from model_pack_tool_panels import ToolPanels,EDITABLE

try:
    from PySide6 import QtWidgets, QtCore, QtGui
    UniversalActionGroup = QtGui.QActionGroup
except ImportError:
    from PySide2 import QtWidgets, QtCore, QtGui
    UniversalActionGroup = QtWidgets.QActionGroup

WORKSPACE_WINDOW_NAME = "ModelPackToolboxUI"


def get_maya_main_window():
    for widget in QtWidgets.QApplication.topLevelWidgets():
        if widget.objectName() == 'MayaWindow':
            return widget
    return None


# --------------------------------------------------------------------------- #
#  small vector helpers (plain tuples, no numpy dependency)
# --------------------------------------------------------------------------- #
def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _scale(a, s):
    return (a[0] * s, a[1] * s, a[2] * s)


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _norm(a):
    return math.sqrt(_dot(a, a))


def _normalize(a):
    n = _norm(a)
    if n < 1e-12:
        return (0.0, 0.0, 0.0)
    return (a[0] / n, a[1] / n, a[2] / n)


def _dist(a, b):
    return _norm(_sub(a, b))


def _centroid(pts):
    n = len(pts)
    sx = sy = sz = 0.0
    for p in pts:
        sx += p[0]; sy += p[1]; sz += p[2]
    return (sx / n, sy / n, sz / n)


def _newell(pts):
    """Robust polygon normal (works for non-planar rings on bends)."""
    nx = ny = nz = 0.0
    n = len(pts)
    for i in range(n):
        c = pts[i]
        d = pts[(i + 1) % n]
        nx += (c[1] - d[1]) * (c[2] + d[2])
        ny += (c[2] - d[2]) * (c[0] + d[0])
        nz += (c[0] - d[0]) * (c[1] + d[1])
    return (nx, ny, nz)


def _perp(n):
    """Some vector perpendicular to n."""
    a = (1.0, 0.0, 0.0)
    if abs(_dot(n, a)) > 0.9:
        a = (0.0, 1.0, 0.0)
    return _normalize(_cross(n, a))


def _avg(vals):
    return sum(vals) / len(vals) if vals else 0.0


def _interp1d(x, xs, ys):
    """Piecewise linear over ascending xs."""
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    for i in range(1, len(xs)):
        if x <= xs[i]:
            dx = xs[i] - xs[i - 1]
            t = (x - xs[i - 1]) / dx if dx > 1e-12 else 0.0
            return ys[i - 1] + t * (ys[i] - ys[i - 1])
    return ys[-1]


def _interp_angle(th, aa2, rr2):
    lo = aa2[0]
    while th < lo:
        th += 2.0 * math.pi
    while th >= lo + 2.0 * math.pi:
        th -= 2.0 * math.pi
    return _interp1d(th, aa2, rr2)


# --------------------------------------------------------------------------- #
#  mesh data container + topology walk
# --------------------------------------------------------------------------- #
class _MeshData(object):
    def __init__(self, dag,matrix=None):
        self.dag = dag
        self.fn = om.MFnMesh(dag)

        pts = self.fn.getPoints(om.MSpace.kWorld if isinstance(dag,om.MDagPath) else om.MSpace.kObject)
        if matrix is not None:pts=[p*matrix for p in pts]
        self.P = [(pts[i].x, pts[i].y, pts[i].z) for i in range(len(pts))]

        counts, connects = self.fn.getVertices()
        self.faceVerts = []
        idx = 0
        for c in counts:
            self.faceVerts.append([connects[idx + t] for t in range(c)])
            idx += c

        # edge -> set(faceId)
        self.edgeFaces = {}
        for fid, fv in enumerate(self.faceVerts):
            k = len(fv)
            for t in range(k):
                a = fv[t]
                b = fv[(t + 1) % k]
                key = (a, b) if a < b else (b, a)
                self.edgeFaces.setdefault(key, set()).add(fid)

        # vertex -> set(neighbours)
        self.vertNbrs = {}
        for (a, b) in self.edgeFaces:
            self.vertNbrs.setdefault(a, set()).add(b)
            self.vertNbrs.setdefault(b, set()).add(a)

        # per-face-corner UV: faceCornerUV[faceId][vertexId] = (u, v).
        # Reading UVs per corner (not per vertex) preserves every UV cut,
        # including several shells split along the tube length.
        self.faceCornerUV = [dict() for _ in self.faceVerts]
        try:
            uArr, vArr = self.fn.getUVs()
            uvCounts, uvIds = self.fn.getAssignedUVs()
            ptr = 0
            for fid, fv in enumerate(self.faceVerts):
                k = uvCounts[fid] if fid < len(uvCounts) else 0
                for t in range(k):
                    self.faceCornerUV[fid][fv[t]] = (uArr[uvIds[ptr + t]],
                                                     vArr[uvIds[ptr + t]])
                ptr += k
        except Exception:
            pass

    def edge_faces(self, a, b):
        key = (a, b) if a < b else (b, a)
        return self.edgeFaces.get(key, set())

    def is_open_ring(self, ring_ids):
        a, b = ring_ids[0], ring_ids[1]
        return len(self.edge_faces(a, b)) <= 1


def _order_seam(md, seam_edge_ids):
    """
    Ordered rail of seam vertices (one per ring).
    Returns (rail, closed). `closed` is True for a closed-loop tube (the seam
    is a closed edge loop with no endpoints); rail then holds M unique verts
    without repeating the start.
    """
    itEdge = om.MItMeshEdge(md.dag)
    pairs = []
    for eid in seam_edge_ids:
        itEdge.setIndex(eid)
        pairs.append((itEdge.vertexId(0), itEdge.vertexId(1)))

    adj = {}
    for a, b in pairs:
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, []).append(a)

    endpoints = [v for v, nb in adj.items() if len(nb) == 1]
    if len(endpoints) == 2:
        closed = False
        start = min(endpoints)
    elif len(endpoints) == 0:
        closed = True
        start = min(adj.keys())
    else:
        raise RuntimeError(
            u"Шов должен быть цельным продольным лупом рёбер "
            u"(найдено свободных концов: {}).".format(len(endpoints)))

    rail = [start]
    prev, cur = None, start
    guard = 0
    while True:
        nxts = [x for x in adj[cur] if x != prev]
        if not nxts:
            break
        nxt = nxts[0]
        if nxt == start:          # closed loop wrapped back
            break
        rail.append(nxt)
        prev, cur = cur, nxt
        guard += 1
        if guard > len(pairs) + 4:
            break

    expect = len(pairs) if closed else len(pairs) + 1
    if len(rail) != expect:
        raise RuntimeError(u"Не удалось упорядочить шов (ветвление?).")
    return rail, closed


def _march_band(md, v0, v1, ref):
    """
    Walk one ring-band (strip between two adjacent rings), starting from the
    seam rail edge (v0, v1) using `ref` (ring_j col_1) to pick winding.
    Returns (bottom_columns, top_columns, band_faces); band_faces[i] is the
    quad between column i and column i+1 (wrap), N faces total.
    """
    faces = md.edge_faces(v0, v1)
    F = None
    for f in faces:
        if ref in md.faceVerts[f]:
            F = f
            break
    if F is None:
        raise RuntimeError(u"Не найдена стартовая грань для обхода кольца.")

    bot, top = [v0], [v1]
    band_faces = []
    curE = (v0, v1)
    curF = F
    guard = 0
    while True:
        band_faces.append(curF)
        fv = md.faceVerts[curF]
        if len(fv) != 4:
            raise RuntimeError(u"Тело трубы должно быть из квадов (найден "
                               u"{}-угольник).".format(len(fv)))
        k = 4
        i0 = fv.index(curE[0])
        n0a, n0b = fv[(i0 - 1) % k], fv[(i0 + 1) % k]
        b1 = n0a if n0a != curE[1] else n0b
        i1 = fv.index(curE[1])
        n1a, n1b = fv[(i1 - 1) % k], fv[(i1 + 1) % k]
        t1 = n1a if n1a != curE[0] else n1b

        if b1 == v0:
            break
        bot.append(b1)
        top.append(t1)

        nf = [f for f in md.edge_faces(b1, t1) if f != curF]
        if not nf:
            raise RuntimeError(u"Обрыв ряда при обходе (не grid-топология).")
        curF = nf[0]
        curE = (b1, t1)
        guard += 1
        if guard > 100000:
            raise RuntimeError(u"Переполнение обхода кольца.")
    return bot, top, band_faces


def _extract_rings(md, rail, closed):
    """
    Return (rings, bands, N).
      rings : list of M rings, each a list of N vertex ids (consistent columns).
      bands : list of tuples (bot_ids, top_ids, band_faces); M-1 for an open
              tube, M for a closed-loop tube (last band wraps ring M-1 -> 0).
    """
    M = len(rail)
    if M < 2:
        raise RuntimeError(u"Слишком мало колец.")

    # Derive the winding reference from an actual quad face on the first seam
    # edge, so the march's start face is guaranteed to exist even when the seam
    # endpoint vertex carries extra edges (bevels / cap transitions).
    quad_faces = [f for f in md.edge_faces(rail[0], rail[1])
                  if len(md.faceVerts[f]) == 4]
    if not quad_faces:
        raise RuntimeError(u"Первое ребро шва не граничит с квад-гранью тела "
                           u"(торец/бевел на конце шва?). Начни шов с ровного "
                           u"участка трубы.")
    fv0 = md.faceVerts[quad_faces[0]]
    k0 = len(fv0)
    i0 = fv0.index(rail[0])
    cand = (fv0[(i0 - 1) % k0], fv0[(i0 + 1) % k0])
    ref = cand[0] if cand[0] != rail[1] else cand[1]

    nbands = M if closed else M - 1
    bands = []
    for j in range(nbands):
        v0 = rail[j]
        v1 = rail[(j + 1) % M]
        bot, top, band_faces = _march_band(md, v0, v1, ref)
        bands.append((bot, top, band_faces))
        ref = top[1]  # ring col_1 for next band

    if closed:
        rings = [bands[j][0] for j in range(nbands)]
    else:
        rings = [bands[0][0]] + [b[1] for b in bands]

    N = len(rings[0])
    for r in rings:
        if len(r) != N:
            raise RuntimeError(u"Кольца имеют разное число вершин "
                               u"(не однородная grid-топология).")
    return rings, bands, N


# --------------------------------------------------------------------------- #
#  per-ring resample (geometry) + UV source arrays
# --------------------------------------------------------------------------- #
def _resample_ring_geo(md, ring_ids, Mt):
    Pn = [md.P[v] for v in ring_ids]
    C = _centroid(Pn)
    n = _normalize(_newell(Pn))
    if _norm(n) < 1e-9:
        n = (0.0, 1.0, 0.0)

    d0 = _sub(Pn[0], C)
    d0p = _sub(d0, _scale(n, _dot(d0, n)))
    if _norm(d0p) < 1e-9:
        d0p = _perp(n)
    u = _normalize(d0p)
    v = _cross(n, u)

    ang, rad = [], []
    for p in Pn:
        d = _sub(p, C)
        du, dv = _dot(d, u), _dot(d, v)
        ang.append(math.atan2(dv, du))
        rad.append(math.hypot(du, dv))

    pairs = sorted(zip(ang, rad))
    aa = [p[0] for p in pairs]
    rr = [p[1] for p in pairs]
    aa2 = aa + [aa[0] + 2.0 * math.pi]
    rr2 = rr + [rr[0]]

    newP = []
    for i in range(Mt):
        th = 2.0 * math.pi * i / Mt
        R = _interp_angle(th, aa2, rr2)
        pos = _add(C, _add(_scale(u, R * math.cos(th)),
                           _scale(v, R * math.sin(th))))
        newP.append(pos)
    return newP, C


def _edge_uv(md, ring_ids, band_faces):
    """
    Source UV samples along one side of a band, read per face-corner so that
    every shell cut and the around-seam are preserved exactly.
    Returns (xs, us, vs) as open polylines of length N+1 (seam doubled).
      xs : arc-length fraction around the ring (from source geometry), 0..1.
    """
    N = len(ring_ids)
    Pn = [md.P[v] for v in ring_ids]
    seg = [_dist(Pn[i], Pn[(i + 1) % N]) for i in range(N)]
    total = sum(seg) or 1.0
    xs = [0.0] * N
    acc = 0.0
    for i in range(1, N):
        acc += seg[i - 1]
        xs[i] = acc / total
    xs.append(1.0)

    us, vs = [], []
    for i in range(N):
        uv = md.faceCornerUV[band_faces[i]].get(ring_ids[i], (0.0, 0.0))
        us.append(uv[0])
        vs.append(uv[1])
    # seam high = the wrap corner (column 0) of the last band face
    uvh = md.faceCornerUV[band_faces[N - 1]].get(ring_ids[0], (us[0], vs[0]))
    us.append(uvh[0])
    vs.append(uvh[1])
    return xs, us, vs


def _cap_boundary_uv(md, ring_ids, band_faces_of_ring):
    """
    UV of the original cap boundary, one per ring column, read from the
    cap-side faces (the face on each ring edge that is not the body band face).
    Works for a single n-gon cap or a fan to a center vertex. None if uncapped.
    """
    N = len(ring_ids)
    capUV = [None] * N
    for k in range(N):
        a = ring_ids[k]
        b = ring_ids[(k + 1) % N]
        others = [f for f in md.edge_faces(a, b) if f != band_faces_of_ring[k]]
        if not others:
            return None
        uv = md.faceCornerUV[others[0]].get(a)
        if uv is None:
            return None
        capUV[k] = uv
    return capUV


def _arcfracs(newP):
    Mt = len(newP)
    seg = [_dist(newP[i], newP[(i + 1) % Mt]) for i in range(Mt)]
    tot = sum(seg) or 1.0
    f = [0.0] * Mt
    acc = 0.0
    for i in range(1, Mt):
        acc += seg[i - 1]
        f[i] = acc / tot
    return f


# --------------------------------------------------------------------------- #
#  build the new mesh
# --------------------------------------------------------------------------- #
def _resection_data(dag, seam_edge_ids, target_sides, progress_cb=None,matrix=None):
    if target_sides < 3:
        raise RuntimeError(u"Target sides должно быть >= 3.")

    md = _MeshData(dag,matrix)
    rail, closed = _order_seam(md, seam_edge_ids)
    rings, bands, N = _extract_rings(md, rail, closed)
    M = len(rings)
    nbands = len(bands)      # M-1 for open tube, M for a closed loop
    Mt = target_sides

    if progress_cb:
        progress_cb(5)

    # resample every ring geometry
    rings_new = []
    centroids = []
    for j in range(M):
        newP, C = _resample_ring_geo(md, rings[j], Mt)
        rings_new.append(newP)
        centroids.append(C)
        if progress_cb:
            progress_cb(5 + int(60.0 * (j + 1) / M))

    # source UV along both sides of every band (per-corner -> shell-safe)
    bot_edges = []
    top_edges = []
    for j in range(nbands):
        bot_ids, top_ids, band_faces = bands[j]
        bot_edges.append(_edge_uv(md, bot_ids, band_faces))
        top_edges.append(_edge_uv(md, top_ids, band_faces))

    fnew = [_arcfracs(rings_new[j]) for j in range(M)]

    # --- winding sign (outward normals) --------------------------------- #
    a0, a1 = rings_new[0][0], rings_new[0][1]
    b0, b1 = rings_new[1][0], rings_new[1][1]
    fcenter = _centroid([a0, a1, b1, b0])
    axis = _centroid([centroids[0], centroids[1]])
    ncand = _cross(_sub(a1, a0), _sub(b0, a0))
    flip = _dot(ncand, _sub(fcenter, axis)) < 0.0

    def vid(j, i):
        return j * Mt + i

    # --- vertex positions ----------------------------------------------- #
    verts = om.MFloatPointArray()
    for j in range(M):
        for i in range(Mt):
            p = rings_new[j][i]
            verts.append(om.MFloatPoint(p[0], p[1], p[2]))

    # --- faces + per-corner UVs ----------------------------------------- #
    polyCounts = []
    polyConnects = []
    uvIds = []
    uPool = []
    vPool = []

    def push_uv(u, v):
        uPool.append(u)
        vPool.append(v)
        return len(uPool) - 1

    for j in range(nbands):
        jt = (j + 1) % M      # top ring index (wraps for a closed loop)
        bxs, bus, bvs = bot_edges[j]
        txs, tus, tvs = top_edges[j]
        fb = fnew[j]
        ft = fnew[jt]
        for i in range(Mt):
            i1 = (i + 1) % Mt
            fbi = fb[i]
            fbi1 = 1.0 if i == Mt - 1 else fb[i + 1]
            fti = ft[i]
            fti1 = 1.0 if i == Mt - 1 else ft[i + 1]
            bl = (_interp1d(fbi, bxs, bus), _interp1d(fbi, bxs, bvs))
            br = (_interp1d(fbi1, bxs, bus), _interp1d(fbi1, bxs, bvs))
            tr = (_interp1d(fti1, txs, tus), _interp1d(fti1, txs, tvs))
            tl = (_interp1d(fti, txs, tus), _interp1d(fti, txs, tvs))
            if not flip:
                g = [vid(j, i), vid(j, i1), vid(jt, i1), vid(jt, i)]
                cuv = [bl, br, tr, tl]
            else:
                g = [vid(j, i), vid(jt, i), vid(jt, i1), vid(j, i1)]
                cuv = [bl, tl, tr, br]
            polyCounts.append(4)
            polyConnects.extend(g)
            for (uu, vv) in cuv:
                uvIds.append(push_uv(uu, vv))
        if progress_cb:
            progress_cb(65 + int(20.0 * (j + 1) / nbands))

    # --- caps (auto) ---------------------------------------------------- #
    cap_face_ids = []   # indices of cap polygons (appended last)

    def add_cap(j, neighbor_j, band_faces_of_ring):
        ringP = rings_new[j]
        C = centroids[j]
        Cn = centroids[neighbor_j]
        nn = _normalize(_newell(ringP))
        outdir = _normalize(_sub(C, Cn))
        order = list(range(Mt))
        if _dot(nn, outdir) < 0.0:
            order = order[::-1]

        # UV ids per new cap vertex (index i), reordered into gc order after.
        cap_uv_ids = [0] * Mt
        capUV = _cap_boundary_uv(md, rings[j], band_faces_of_ring)
        if capUV is not None:
            # preserve original cap UV: sample its boundary by arc fraction
            oldP = [md.P[v] for v in rings[j]]
            Nn = len(oldP)
            seg = [_dist(oldP[t], oldP[(t + 1) % Nn]) for t in range(Nn)]
            tot = sum(seg) or 1.0
            xs = [0.0] * Nn
            acc = 0.0
            for t in range(1, Nn):
                acc += seg[t - 1]
                xs[t] = acc / tot
            xs.append(1.0)
            us = [uv[0] for uv in capUV] + [capUV[0][0]]
            vs = [uv[1] for uv in capUV] + [capUV[0][1]]
            f = fnew[j]
            for i in range(Mt):
                cap_uv_ids[i] = push_uv(_interp1d(f[i], xs, us),
                                        _interp1d(f[i], xs, vs))
        else:
            # planar fallback (uncapped source / unreadable cap UV)
            d0 = _sub(ringP[0], C)
            d0p = _sub(d0, _scale(nn, _dot(d0, nn)))
            if _norm(d0p) < 1e-9:
                d0p = _perp(nn)
            uax = _normalize(d0p)
            vax = _cross(nn, uax)
            maxr = max(_dist(p, C) for p in ringP) or 1.0
            for i in range(Mt):
                d = _sub(ringP[i], C)
                du, dv = _dot(d, uax), _dot(d, vax)
                cap_uv_ids[i] = push_uv(0.5 + 0.5 * du / maxr,
                                        0.5 + 0.5 * dv / maxr)

        gc = [vid(j, i) for i in order]
        cap_uv = [cap_uv_ids[i] for i in order]
        polyCounts.append(Mt)
        polyConnects.extend(gc)
        uvIds.extend(cap_uv)
        cap_face_ids.append(len(polyCounts) - 1)

    if not closed:
        if not md.is_open_ring(rings[0]):
            add_cap(0, 1, bands[0][2])
        if not md.is_open_ring(rings[M - 1]):
            add_cap(M - 1, M - 2, bands[M - 2][2])

    if progress_cb:
        progress_cb(90)

    # --- create -------------------------------------------------------- #
    output = om.MFnMeshData().create()
    meshFn = om.MFnMesh()
    meshFn.create(verts, polyCounts, polyConnects,parent=output)
    meshFn.setUVs(uPool, vPool)
    meshFn.assignUVs(polyCounts, uvIds)
    meshFn.updateSurface()
    return output,M,N,Mt,cap_face_ids,centroids


def resection(dag_str, seam_edge_ids, target_sides, progress_cb=None):
    sel=om.MSelectionList();sel.add(dag_str);dag=sel.getDagPath(0)
    if dag.node().hasFn(om.MFn.kTransform):dag.extendToShape()
    output,M,N,Mt,cap_face_ids,centroids=_resection_data(dag,seam_edge_ids,target_sides,progress_cb)
    meshFn=om.MFnMesh();meshFn.copy(output)

    newShape = meshFn.name()
    newT = cmds.listRelatives(newShape, parent=True, fullPath=True)[0]

    origShape = dag.fullPathName()
    origT = cmds.listRelatives(origShape, parent=True, fullPath=True)
    base = (origT[0] if origT else origShape).split('|')[-1]
    newT = cmds.rename(newT, base + "_resect")

    # shader from original
    try:
        sgs = cmds.listConnections(origShape, type='shadingEngine') or []
        sg = sgs[0] if sgs else 'initialShadingGroup'
        cmds.sets(newT, e=True, forceElement=sg)
    except Exception:
        pass

    # weld coincident UVs (per-corner UVs -> shared points) while keeping the
    # real cuts (seam + shell borders have non-coincident UVs, so they stay).
    try:
        cmds.polyMergeUV(newT, distance=1e-4, ch=False)
    except Exception:
        pass

    cmds.polySoftEdge(newT, angle=180, ch=False)  # body: all edges soft

    # caps: rim edges hard + triangulated fill
    if cap_face_ids:
        try:
            shp = cmds.listRelatives(newT, shapes=True, fullPath=True,
                                     type="mesh")[0]
            cap_faces = ["%s.f[%d]" % (shp, cf) for cf in cap_face_ids]
            rim = cmds.polyListComponentConversion(cap_faces, ff=True, te=True)
            rim = cmds.filterExpand(rim, sm=32) or []
            if rim:
                cmds.polySoftEdge(rim, angle=0, ch=False)   # harden cap rim
            cmds.polyTriangulate(cap_faces, ch=False)       # triangulate caps
        except Exception:
            pass

    # conform normals + orient outward (robust; independent of build winding,
    # covers body and the triangulated caps)
    try:
        cmds.polyNormal(newT, normalMode=3, ch=False)       # conform consistent
        shp = cmds.listRelatives(newT, shapes=True, fullPath=True,
                                 type="mesh")[0]
        d2 = om.MSelectionList()
        d2.add(shp)
        mfn2 = om.MFnMesh(d2.getDagPath(0))
        nrm = mfn2.getPolygonNormal(0, om.MSpace.kWorld)     # a body face
        p2 = mfn2.getPoints(om.MSpace.kWorld)
        vids0 = mfn2.getPolygonVertices(0)
        cx = sum(p2[v].x for v in vids0) / len(vids0)
        cy = sum(p2[v].y for v in vids0) / len(vids0)
        cz = sum(p2[v].z for v in vids0) / len(vids0)
        ax = (centroids[0][0] + centroids[1][0]) * 0.5
        ay = (centroids[0][1] + centroids[1][1]) * 0.5
        az = (centroids[0][2] + centroids[1][2]) * 0.5
        outward = nrm.x * (cx - ax) + nrm.y * (cy - ay) + nrm.z * (cz - az)
        if outward < 0.0:
            cmds.polyNormal(newT, normalMode=0, ch=False)    # reverse all
    except Exception:
        pass

    cmds.select(newT, r=True)

    if progress_cb:
        progress_cb(100)
    return newT, M, N, Mt


# --------------------------------------------------------------------------- #
#  TOOLBOX BUTTONS AND CONTEXT-MENU OPTION WIDGETS
# --------------------------------------------------------------------------- #
class ModelPackButton(QtWidgets.QPushButton):
    """Keep the icon at the left inset and the caption at the right inset."""

    _PADDING = 6
    _ICON_COLUMN = 30
    _GAP = 4

    def sizeHint(self):
        size = super().sizeHint()
        width = (self.fontMetrics().horizontalAdvance(self.text()) + self._ICON_COLUMN
                 + self._GAP + 2 * self._PADDING)
        size.setWidth(max(size.width(), width))
        return size

    def paintEvent(self, event):
        option = QtWidgets.QStyleOptionButton()
        self.initStyleOption(option)
        caption, icon = option.text, QtGui.QIcon(option.icon)
        option.text = ""
        option.icon = QtGui.QIcon()
        painter = QtWidgets.QStylePainter(self)
        painter.drawControl(QtWidgets.QStyle.CE_PushButton, option)
        contents = self.rect().adjusted(self._PADDING, 0, -self._PADDING, 0)
        if option.state & QtWidgets.QStyle.State_Sunken:
            contents.translate(self.style().pixelMetric(QtWidgets.QStyle.PM_ButtonShiftHorizontal, option, self),
                               self.style().pixelMetric(QtWidgets.QStyle.PM_ButtonShiftVertical, option, self))
        icon_rect = QtCore.QRect(contents.left(), contents.center().y() - self.iconSize().height() // 2,
                                 self.iconSize().width(), self.iconSize().height())
        mode = QtGui.QIcon.Normal if self.isEnabled() else QtGui.QIcon.Disabled
        state = QtGui.QIcon.On if self.isChecked() else QtGui.QIcon.Off
        icon.paint(painter, icon_rect, QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, mode, state)
        text_rect = contents.adjusted(self._ICON_COLUMN + self._GAP, 0, 0, 0)
        self.style().drawItemText(painter, text_rect, QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter,
                                  option.palette, self.isEnabled(), caption, QtGui.QPalette.ButtonText)


class MirrorMenuWidget(QtWidgets.QWidget):
    def __init__(self, parent_tool=None, parent=None, is_duplicate=False):
        super(MirrorMenuWidget, self).__init__(parent)
        self.parent_tool = parent_tool
        self.is_duplicate = is_duplicate
        self.build_ui()

    def build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        axis_layout = QtWidgets.QHBoxLayout()
        axis_layout.setSpacing(2)
        self.bg_axis = QtWidgets.QButtonGroup(self)
        for i, ax in enumerate(["X", "Y", "Z"]):
            btn = QtWidgets.QPushButton(ax)
            btn.setCheckable(True)
            btn.setMinimumHeight(24)
            self.bg_axis.addButton(btn, i)
            axis_layout.addWidget(btn)
        layout.addLayout(axis_layout)

        space_layout = QtWidgets.QHBoxLayout()
        space_layout.setSpacing(2)
        self.bg_space = QtWidgets.QButtonGroup(self)
        for i, sp in enumerate(["World", "Local"]):
            btn = QtWidgets.QPushButton(sp)
            btn.setCheckable(True)
            btn.setMinimumHeight(24)
            self.bg_space.addButton(btn, i)
            space_layout.addWidget(btn)
        layout.addLayout(space_layout)

        if not self.is_duplicate:
            row=QtWidgets.QHBoxLayout();row.addWidget(QtWidgets.QLabel('Сварка',self))
            self.threshold=QtWidgets.QDoubleSpinBox(self);self.threshold.setRange(0,1000000)
            self.threshold.setDecimals(6);self.threshold.setKeyboardTracking(False)
            self.threshold.setValue(float(cmds.optionVar(q='ModelPack_MirrorThreshold')))
            self.threshold.valueChanged.connect(self.on_threshold_changed)
            row.addWidget(self.threshold);layout.addLayout(row)

        c_axis = cmds.optionVar(q="ModelPack_MirrorAxis")
        self._axis = c_axis
        self._direction = (cmds.optionVar(q="ModelPack_MirrorDirection")
                           if cmds.optionVar(exists="ModelPack_MirrorDirection") else 0)
        if self.bg_axis.button(c_axis):
            self.bg_axis.button(c_axis).setChecked(True)
        self._sync_axis_labels()
        c_space = cmds.optionVar(q="ModelPack_MirrorSpace")
        if self.bg_space.button(c_space):
            self.bg_space.button(c_space).setChecked(True)

        self.bg_axis.idClicked.connect(self.on_axis_changed)
        self.bg_space.idClicked.connect(self.on_space_changed)

    def on_axis_changed(self, idx):
        if not self.is_duplicate:
            self._direction = 1 - self._direction if idx == self._axis else 0
            cmds.optionVar(intValue=("ModelPack_MirrorDirection", self._direction))
        self._axis = idx
        cmds.optionVar(intValue=("ModelPack_MirrorAxis", idx))
        self._sync_axis_labels()
        self._notify()

    def _sync_axis_labels(self):
        for idx, axis in enumerate(("X", "Y", "Z")):
            negative = not self.is_duplicate and idx == self._axis and self._direction == 1
            button = self.bg_axis.button(idx)
            button.setText(("-" if negative else "") + axis)
            if not self.is_duplicate:
                destination = ("-" if negative else "+") + axis
                source = ("+" if negative else "-") + axis
                button.setToolTip(f"Отражение в {destination}: сторона {source} копируется на {destination}. "
                                  "Повторное нажатие меняет сторону.")
            else:button.setToolTip('Выбрать плоскость отражения с нормалью по '+axis+'.')

    def on_space_changed(self, idx):
        cmds.optionVar(intValue=("ModelPack_MirrorSpace", idx))
        self._notify()

    def on_threshold_changed(self,value):
        cmds.optionVar(floatValue=('ModelPack_MirrorThreshold',value));self._notify()

    def _notify(self):
        if self.parent_tool is not None:
            self.parent_tool._modifier_settings_changed('mirror_dup' if self.is_duplicate else 'mirror_cut')

    def on_thresh_clicked(self):
        if self.parent_tool:
            self.parent_tool.set_threshold_dialog()


class ResectMenuWidget(QtWidgets.QWidget):
    def __init__(self, parent=None,on_change=None):
        super(ResectMenuWidget, self).__init__(parent)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        layout.addWidget(QtWidgets.QLabel("Target sides:"))
        self.spin = QtWidgets.QSpinBox()
        self.spin.setRange(3, 256)
        self.spin.setKeyboardTracking(False)
        self.spin.setValue(int(cmds.optionVar(q="ModelPack_ResectSides")))
        self.spin.valueChanged.connect(
            lambda v: cmds.optionVar(intValue=("ModelPack_ResectSides", v)))
        layout.addWidget(self.spin)
        if on_change is not None:self.spin.valueChanged.connect(on_change)


# --------------------------------------------------------------------------- #
#  MAIN TOOLBOX UI
# --------------------------------------------------------------------------- #
class ModelPackToolbox(ToolPanels,QtWidgets.QDialog):

    def __init__(self, parent=get_maya_main_window()):
        super(ModelPackToolbox, self).__init__(parent)
        self.setWindowTitle("Model Pack")
        self.setObjectName(WORKSPACE_WINDOW_NAME)
        self.setWindowFlags(self.windowFlags() ^ QtCore.Qt.WindowContextHelpButtonHint)
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        self.init_option_vars()
        self._active_tool = None
        self._changing_tool = False
        self._last_context = cmds.currentCtx()
        self._hard_edge_session = None
        self._hard_edge_chunk = False
        self._hard_edge_compact_size = None
        self._edit_panel=None;self._edit_kind=None;self._modifier_nodes=[];self._edit_compact_size=None
        self.build_ui()
        self._bevel_group_states = []
        self._bevel_group_callbacks = [om.MEventMessage.addEventCallback(event, self._restore_bevel_groups)
                                       for event in ("Undo", "Redo")]
        self._bevel_group_callbacks += [om.MEventMessage.addEventCallback(event,self._restore_modifier_controls)
                                        for event in ('Undo','Redo')]
        self.slide_timer = QtCore.QTimer(self)
        self.slide_timer.timeout.connect(self._sync_slide_button)
        self.slide_timer.start(250)

    def init_option_vars(self):
        if not cmds.optionVar(exists="ModelPack_SelectionSkip"):
            cmds.optionVar(intValue=("ModelPack_SelectionSkip",1))
        if not cmds.optionVar(exists="ModelPack_MirrorAxis"):
            cmds.optionVar(intValue=("ModelPack_MirrorAxis", 0))
        if not cmds.optionVar(exists="ModelPack_MirrorSpace"):
            cmds.optionVar(intValue=("ModelPack_MirrorSpace", 0))
        if not cmds.optionVar(exists="ModelPack_MirrorDirection"):
            cmds.optionVar(intValue=("ModelPack_MirrorDirection", 0))
        if not cmds.optionVar(exists="ModelPack_MirrorThreshold"):
            cmds.optionVar(floatValue=("ModelPack_MirrorThreshold", 0.001))
        if not cmds.optionVar(exists="ModelPack_ResectSides"):
            cmds.optionVar(intValue=("ModelPack_ResectSides", 6))
        if not cmds.optionVar(exists="ModelPack_HardEdgeAngle"):
            cmds.optionVar(floatValue=("ModelPack_HardEdgeAngle", 30.0))
        if not cmds.optionVar(exists="ModelPack_SlideUniformDefaultV2"):
            cmds.optionVar(intValue=("ModelPack_CloneOffsetMode", 1))
            cmds.optionVar(intValue=("ModelPack_BevelOffsetMode", 1))
            cmds.optionVar(intValue=("ModelPack_SlideUniformDefaultV2", 1))
        for key in ("ModelPack_CloneEdgeCount", "ModelPack_BevelEdgeCount"):
            if not cmds.optionVar(exists=key):
                cmds.optionVar(intValue=(key, 1))
        for key in ("ModelPack_CloneSecondaryPercent", "ModelPack_BevelSecondaryPercent"):
            if not cmds.optionVar(exists=key):
                cmds.optionVar(intValue=(key, 50))
        for key, value in (("ModelPack_FindBevelSensitivity", 1),
                           ("ModelPack_FindBevelCorners", 1),
                           ("ModelPack_FindBevelReadyOnly", 0)):
            if not cmds.optionVar(exists=key):
                cmds.optionVar(intValue=(key, value))

    def build_ui(self):
        layout = QtWidgets.QGridLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(5)

        self.icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")

        buttons_data = [
            ("Combine", "combine.png", self.tool_combine, None),
            ("Separate", "Separate.png", self.tool_separate, None),
            ("Extract Faces", "extract_faces.png", self.tool_extract_faces, None),
            ("Detach Faces", "detach_faces.png", self.tool_detach_faces, None),
            ("Mirror Cut", "mirror.png", self.tool_mirror, None),
            ("Mirror Dup", "mirror_duplicate.png", self.tool_mirror_duplicate, None),
            ("Reset Tube", "reset_tube.png", self.tool_resect_tube, None),
            ("Hard Edges", "hard_edge.png", self.tool_hard_edges, None),
            ("Clone Slide", "slide_inactive.png", self.tool_slide, None),
            ("Slide Bevel", "slide_bevel_off.png", self.tool_slide_bevel,
             None),
            ("Bevel", "Bevel.png", self.tool_gesture_bevel, self.show_gesture_bevel_menu),
            ("UnBevel", "UnBevel.png", self.tool_unbevel, None),
            ("Select", "selection.svg", self.tool_selection, self.show_selection_menu),
            ("Plane Deform", "Plane_Deformation.png", self.tool_linear_shift, None),
        ]
        positions = [(i // 2, i % 2) for i in range(len(buttons_data))]

        for i, data in enumerate(buttons_data):
            label, icon_file, cmd, context_cmd = data
            btn = ModelPackButton(label)
            btn.setMinimumSize(100, 40)
            full_icon_path = os.path.join(self.icon_path, icon_file)
            if os.path.exists(full_icon_path):
                btn.setIcon(QtGui.QIcon(full_icon_path))
                btn.setIconSize(QtCore.QSize(30, 30) if label in ("Clone Slide", "Slide Bevel")
                                else QtCore.QSize(24, 24))
            btn.clicked.connect(lambda checked=False,command=cmd,name=label:self._run_tool(name,command))
            if label in ('Mirror Cut','Mirror Dup','Reset Tube'):btn.setCheckable(True)
            if label == "Clone Slide":
                self.slide_button = btn
                btn.setCheckable(True)
                btn.setChecked(False)
            if label == "Slide Bevel":
                self.bevel_button = btn
                btn.setCheckable(True)
                btn.setChecked(False)
            if label == "Bevel":
                self.gesture_bevel_button = btn
                btn.setCheckable(True)
                btn.setToolTip("Тяни: ширина · Shift: сегменты · Ctrl: форма · Shift+Ctrl: локально. Enter — применить, Esc — отменить.")
            if label == "UnBevel":
                self.unbevel_button = btn
                btn.setCheckable(True)
                btn.setToolTip("Открыть удаление и поиск фасок со списком отдельных участков.")
            if label == "Select":
                self.selection_button = btn
                btn.setCheckable(True)
                btn.setToolTip("Область · Grow/Shrink · выбор с шагом по кольцу. ПКМ — команды.")
            if label == "Hard Edges":
                self.hard_edge_button = btn
                btn.setCheckable(True)
                btn.setToolTip("Выделение рёбер по углу. Панель снизу: угол и Harden Edge.")
            if label == "Plane Deform":
                self.linear_shift_button = btn
                btn.setCheckable(True)
                btn.setToolTip("Наклон плоскости сечения со скольжением по соседним рёбрам. ПКМ — положение рамки: локально / глобально. Enter — выйти, Esc — отменить жест.")
            if context_cmd:
                btn.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
                btn.customContextMenuRequested.connect(context_cmd)
            layout.addWidget(btn, positions[i][0], positions[i][1])

        self.unbevel_panel = QtWidgets.QWidget(self)
        self.unbevel_panel.setObjectName("unbevelActions")
        unbevel_layout = QtWidgets.QVBoxLayout(self.unbevel_panel)
        unbevel_layout.setContentsMargins(0, 5, 0, 0)
        unbevel_layout.setSpacing(5)
        actions = QtWidgets.QHBoxLayout()
        unbevel_layout.addLayout(actions)
        actions.setSpacing(5)
        self.remove_bevel_button = QtWidgets.QPushButton("Убрать фаски", self.unbevel_panel)
        self.remove_bevel_button.setMinimumHeight(32)
        self.remove_bevel_button.setToolTip("Сразу убрать выделенные фаски. Ctrl+Z — вернуть результат.")
        self.remove_bevel_button.clicked.connect(self.tool_remove_bevels)
        self.find_bevel_button = QtWidgets.QPushButton("Найти фаски", self.unbevel_panel)
        self.find_bevel_button.setMinimumHeight(32)
        self.find_bevel_button.setToolTip("Выделен полигон — найти только его фаску. Выбран объект — найти все фаски. ПКМ — настройки поиска.")
        self.find_bevel_button.clicked.connect(lambda checked=False: self.tool_find_bevels())
        self.find_bevel_button.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.find_bevel_button.customContextMenuRequested.connect(self.show_unbevel_menu)
        actions.addWidget(self.remove_bevel_button)
        actions.addWidget(self.find_bevel_button)
        self.bevel_groups_label = QtWidgets.QLabel(self.unbevel_panel)
        self.bevel_groups_list = QtWidgets.QListWidget(self.unbevel_panel)
        self.bevel_groups_list.setObjectName("foundBevelGroups")
        self.bevel_groups_list.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.bevel_groups_list.setMinimumHeight(100)
        self.bevel_groups_list.setMaximumHeight(180)
        self.bevel_groups_list.setToolTip("Клик — выбрать фаску. Ctrl / Shift — выбрать несколько фасок.")
        self.bevel_groups_list.itemSelectionChanged.connect(self._select_bevel_groups)
        unbevel_layout.addWidget(self.bevel_groups_label)
        unbevel_layout.addWidget(self.bevel_groups_list)
        self._bevel_groups = ()
        self._bevel_group_topology = {}
        self.bevel_groups_label.hide()
        self.bevel_groups_list.hide()
        panel_row = (len(buttons_data)+1)//2
        self._panel_row=panel_row
        layout.addWidget(self.unbevel_panel, panel_row, 0, 1, 2)
        self.unbevel_panel.hide()
        self._unbevel_compact_size = None
        self.selection_panel = QtWidgets.QWidget(self)
        self.selection_panel.setObjectName("selectionActions")
        selection_layout = QtWidgets.QGridLayout(self.selection_panel)
        selection_layout.setContentsMargins(0,5,0,0)
        commands = (("Область",self.tool_selection_region,0,0,2),
                    ("Grow",lambda:self.tool_selection_grow(),1,0,1),
                    ("Shrink",lambda:self.tool_selection_grow(shrink=True),1,1,1),
                    ("Вдоль кольца",lambda:self.tool_selection_periodic(),2,0,1),
                    ("Поперёк кольца",lambda:self.tool_selection_periodic(across=True),2,1,1))
        self.selection_actions = {}
        for label,command,row,col,span in commands:
            button = QtWidgets.QPushButton(label,self.selection_panel)
            button.setMinimumHeight(32);button.clicked.connect(command)
            self.selection_actions[label] = button
            selection_layout.addWidget(button,row,col,1,span)
        self.selection_actions["Область"].setCheckable(True)
        self.selection_actions["Область"].setToolTip("Выделите контур полигонами и кликните внутри. Shift сохраняет контур.")
        for label in ("Grow","Shrink"):
            self.selection_actions[label].setToolTip("Без модификаторов — Maya · Ctrl — широкое направление · Shift — узкое.")
        for label in ("Вдоль кольца","Поперёк кольца"):
            self.selection_actions[label].setToolTip("Один полигон — всё кольцо; полоса — проредить выделение. Число пропусков задаётся под кнопками.")
        skip_row = QtWidgets.QWidget(self.selection_panel)
        skip_layout = QtWidgets.QHBoxLayout(skip_row)
        skip_layout.setContentsMargins(0,0,0,0)
        skip_layout.addWidget(QtWidgets.QLabel("Пропускать полигонов",skip_row))
        self.selection_skip = QtWidgets.QSpinBox(skip_row)
        self.selection_skip.setRange(0,1000)
        self.selection_skip.setValue(int(cmds.optionVar(q="ModelPack_SelectionSkip")))
        self.selection_skip.setToolTip("1 — каждый второй, 2 — каждый третий; 0 — все полигоны.")
        self.selection_skip.valueChanged.connect(lambda value:cmds.optionVar(intValue=("ModelPack_SelectionSkip",value)))
        skip_layout.addWidget(self.selection_skip)
        selection_layout.addWidget(skip_row,3,0,1,2)
        layout.addWidget(self.selection_panel,panel_row,0,1,2)
        self.selection_panel.hide()
        self._selection_compact_size = None
        self.hard_edge_panel = QtWidgets.QWidget(self)
        self.hard_edge_panel.setObjectName('hardEdgeActions')
        hard_layout = QtWidgets.QGridLayout(self.hard_edge_panel)
        hard_layout.setContentsMargins(0,5,0,0)
        self.hard_edge_angle_label = QtWidgets.QLabel(self.hard_edge_panel)
        self.hard_edge_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal,self.hard_edge_panel)
        self.hard_edge_slider.setRange(0,1800)
        self.hard_edge_slider.setSingleStep(10);self.hard_edge_slider.setPageStep(100)
        self.hard_edge_slider.setValue(round(float(cmds.optionVar(q='ModelPack_HardEdgeAngle'))*10))
        self.hard_edge_angle_label.setText(f'Угол ≥ {self.hard_edge_slider.value()/10:.1f}°')
        self.hard_edge_slider.setToolTip('0–180°. Рёбра выделяются и снимаются с выделения при движении ползунка.')
        self.hard_edge_slider.valueChanged.connect(self._hard_edge_angle_changed)
        self.hard_edge_slider.sliderPressed.connect(self._hard_edge_begin_drag)
        self.hard_edge_slider.sliderReleased.connect(self._hard_edge_finish_drag)
        hard_layout.addWidget(self.hard_edge_angle_label,0,0)
        hard_layout.addWidget(self.hard_edge_slider,0,1)
        self.hard_edge_count_label = QtWidgets.QLabel(self.hard_edge_panel)
        self.harden_edge_button = QtWidgets.QPushButton('Harden Edge',self.hard_edge_panel)
        self.harden_edge_button.setMinimumHeight(32)
        self.harden_edge_button.setToolTip('Mesh Display → Harden Edge: сделать выделенные рёбра жёсткими. Ctrl+Z — отменить.')
        self.harden_edge_button.clicked.connect(self.tool_harden_edges)
        hard_layout.addWidget(self.hard_edge_count_label,1,0)
        hard_layout.addWidget(self.harden_edge_button,1,1)
        layout.addWidget(self.hard_edge_panel,panel_row,0,1,2)
        self.hard_edge_panel.hide()
        self._sync_slide_button()

    def _run_tool(self, name, command):
        kind = {"Clone Slide":"clone","Slide Bevel":"slide_bevel","Bevel":"bevel",
                "UnBevel":"unbevel","Select":"selection","Plane Deform":"linear_shift",
                "Hard Edges":"hard_edges","Mirror Cut":"mirror_cut","Mirror Dup":"mirror_dup",
                "Reset Tube":"reset_tube"}.get(name)
        if kind in ('mirror_cut','mirror_dup','reset_tube'):return command()
        self._prepare_tool(kind)
        command()

    def _prepare_tool(self, kind):
        """Only one Model Pack tool and its auxiliary panels are active."""
        if (kind is not None and self._active_tool==kind) or self._changing_tool:return
        self._changing_tool = True
        try:
            if kind!=self._edit_kind:self._close_edit_panel()
            if kind!='hard_edges':self._clear_hard_edges()
            if kind!="bevel":gesture_bevel_ui.shutdown()
            if kind!="clone":slide_ui.deactivate_drag()
            if kind!="slide_bevel":slide_ui.deactivate_bevel_drag()
            if kind!="unbevel":unbevel_ui.shutdown()
            if kind!="selection":selection_ui.shutdown()
            if kind!="linear_shift":linear_shift_ui.shutdown()
            if kind!="unbevel" and not self.unbevel_panel.isHidden():
                self.unbevel_panel.hide();self.layout().activate()
                if self._unbevel_compact_size is not None:self.resize(self._unbevel_compact_size)
                self._unbevel_compact_size = None
            if kind!="selection" and not self.selection_panel.isHidden():
                self.selection_panel.hide();self.layout().activate()
                if self._selection_compact_size is not None:self.resize(self._selection_compact_size)
                self._selection_compact_size = None
            self._active_tool = kind
        finally:self._changing_tool = False

    # ------------------------------------------------------------------ #
    # CONTEXT MENUS
    # ------------------------------------------------------------------ #
    def show_mirror_context_menu(self, pos):
        sender = self.sender()
        menu = QtWidgets.QMenu(self)
        is_dup = "Dup" in sender.text()
        action = QtWidgets.QWidgetAction(menu)
        self.mirror_widget = MirrorMenuWidget(parent_tool=self, parent=menu, is_duplicate=is_dup)
        action.setDefaultWidget(self.mirror_widget)
        menu.addAction(action)
        menu.exec_(sender.mapToGlobal(pos))

    def show_resect_context_menu(self, pos):
        sender = self.sender()
        menu = QtWidgets.QMenu(self)
        action = QtWidgets.QWidgetAction(menu)
        action.setDefaultWidget(ResectMenuWidget(menu))
        menu.addAction(action)
        menu.exec_(sender.mapToGlobal(pos))

    def set_threshold_dialog(self):
        current_val = cmds.optionVar(q="ModelPack_MirrorThreshold")
        val, ok = QtWidgets.QInputDialog.getDouble(
            self, "Weld Threshold", "Enter threshold:",
            current_val, 0.0, 100.0, 4)
        if ok:
            cmds.optionVar(floatValue=("ModelPack_MirrorThreshold", val))

    # ------------------------------------------------------------------ #
    # UTILITIES
    # ------------------------------------------------------------------ #
    def get_parent(self, obj):
        parent = cmds.listRelatives(obj, parent=True, fullPath=True)
        return parent[0] if parent else None

    def get_auto_mirror_direction(self, obj, axis, is_world):
        if is_world:
            bb = cmds.xform(obj, q=True, boundingBox=True, worldSpace=True)
            center = (bb[axis] + bb[axis + 3]) / 2.0
        else:
            selection = om.MSelectionList()
            selection.add(obj)
            path = selection.getDagPath(0)
            if path.node().hasFn(om.MFn.kTransform):
                path.extendToShape()
            # xform's boundingBox query returns transformed bounds even with
            # objectSpace=True. The mesh DAG bounds are in object coordinates.
            bounds = om.MFnMesh(path).boundingBox
            center = (bounds.min[axis] + bounds.max[axis]) / 2.0
        pivot = 0.0 if is_world else cmds.xform(obj, q=True, objectSpace=True, rotatePivot=True)[axis]
        # Maya's negative direction keeps the positive half and mirrors it
        # toward the negative side; compare against the local pivot, not zero.
        return axis * 2 + (1 if center >= pivot else 0)

    # ------------------------------------------------------------------ #
    # TOOLS — Model Pack
    # ------------------------------------------------------------------ #
    def tool_combine(self):
        sel = cmds.ls(sl=True, l=True)
        if not sel or len(sel) < 2:
            cmds.warning("Select at least two objects to combine.")
            return
        first_obj_name = sel[0].split('|')[-1]
        target_parent = self.get_parent(sel[0])
        cmds.undoInfo(openChunk=True, chunkName="ModelPack_Combine")
        try:
            combined = cmds.polyUnite(sel, ch=True)[0]
            cmds.delete(combined, ch=True)
            combined = cmds.rename(combined, f"{first_obj_name}_Combined")
            if target_parent:
                combined = cmds.parent(combined, target_parent)[0]
            cmds.select(combined, r=True)
        except Exception as e:
            cmds.warning(f"Combine failed: {e}")
        finally:
            cmds.undoInfo(closeChunk=True)

    def tool_separate(self):
        sel = cmds.ls(sl=True, l=True)
        if not sel:
            cmds.warning("Select an object to separate.")
            return
        cmds.undoInfo(openChunk=True, chunkName="ModelPack_Separate")
        try:
            final_selection = []
            for obj in sel:
                base_name = obj.split('|')[-1]
                target_parent = self.get_parent(obj)
                cmds.polySeparate(obj, ch=True)
                cmds.delete(ch=True)
                parts = cmds.ls(sl=True, l=True)
                for i, part in enumerate(parts):
                    new_name = cmds.rename(part, f"{base_name}_Part{i+1}")
                    if target_parent:
                        new_name = cmds.parent(new_name, target_parent)[0]
                    else:
                        if self.get_parent(new_name):
                            new_name = cmds.parent(new_name, world=True)[0]
                    final_selection.append(new_name)
            if final_selection:
                cmds.select(final_selection, r=True)
        except Exception as e:
            cmds.warning(f"Separate failed: {e}")
        finally:
            cmds.undoInfo(closeChunk=True)

    def tool_extract_faces(self):
        faces = cmds.filterExpand(sm=34)
        if not faces:
            cmds.warning("Select faces to extract.")
            return
        obj = faces[0].split('.')[0]
        target_parent = self.get_parent(obj)
        cmds.undoInfo(openChunk=True, chunkName="ModelPack_ExtractFaces")
        try:
            dup = cmds.duplicate(obj, name=f"{obj}_Extracted")[0]
            face_indices = [f.split('[')[1].split(']')[0] for f in faces]
            dup_faces_to_keep = [f"{dup}.f[{idx}]" for idx in face_indices]
            cmds.select(dup_faces_to_keep, r=True)
            cmds.InvertSelection()
            cmds.delete()
            cmds.delete(dup, ch=True)
            current_dup_parent = self.get_parent(dup)
            if target_parent and current_dup_parent != target_parent:
                dup = cmds.parent(dup, target_parent)[0]
            cmds.select(dup, r=True)
        except Exception as e:
            cmds.warning(f"Extract Faces failed: {e}")
        finally:
            cmds.undoInfo(closeChunk=True)

    def tool_detach_faces(self):
        faces = cmds.filterExpand(sm=34)
        if not faces:
            cmds.warning("Select faces to detach.")
            return
        obj = faces[0].split('.')[0]
        target_parent = self.get_parent(obj)
        obj_short = obj.split('|')[-1]
        face_indices = [f.split('[')[1].split(']')[0] for f in faces]
        cmds.undoInfo(openChunk=True, chunkName="ModelPack_DetachFaces")
        try:
            dup = cmds.duplicate(obj, name=f"{obj_short}_Detached")[0]
            keep = [f"{dup}.f[{idx}]" for idx in face_indices]
            cmds.select(keep, r=True)
            cmds.InvertSelection()
            to_remove = cmds.filterExpand(sm=34)
            if to_remove:
                cmds.delete(to_remove)
            cmds.delete(dup, ch=True)
            cmds.delete([f"{obj}.f[{idx}]" for idx in face_indices])
            cmds.delete(obj, ch=True)
            cur_parent = self.get_parent(dup)
            if target_parent and cur_parent != target_parent:
                dup = cmds.parent(dup, target_parent)[0]
            cmds.select(dup, r=True)
        except Exception as e:
            cmds.warning(f"Detach Faces failed: {e}")
        finally:
            cmds.undoInfo(closeChunk=True)

    def tool_mirror(self):
        return self._activate_modifier('mirror_cut')
    def _mirror_cut(self, obj, direction, is_world, pivot, threshold):
        cmds.polyMirrorFace(obj, cutMesh=True, axis=direction // 2,
                            axisDirection=direction % 2, mirrorAxis=2 if is_world else 1,
                            mergeMode=1, mergeThreshold=threshold, mergeThresholdType=1,
                            worldSpace=is_world, pivot=pivot, ch=True)

    def tool_mirror_duplicate(self):
        return self._activate_modifier('mirror_dup')
    # ------------------------------------------------------------------ #
    # CLONE + SLIDE
    # ------------------------------------------------------------------ #
    def _clone_mode(self):
        return ("relative", "uniform")[int(cmds.optionVar(q="ModelPack_CloneOffsetMode"))]

    def _edge_count(self, bevel=False):
        key = "ModelPack_BevelEdgeCount" if bevel else "ModelPack_CloneEdgeCount"
        return min(16, max(1, int(cmds.optionVar(q=key))))

    def _set_edge_count(self, count, bevel=False):
        key = "ModelPack_BevelEdgeCount" if bevel else "ModelPack_CloneEdgeCount"
        cmds.optionVar(intValue=(key, count))
        slide_ui.set_edge_count(count, bevel=bevel)

    def _edge_bias(self, bevel=False):
        key = ("ModelPack_BevelSecondaryPercent" if bevel else
               "ModelPack_CloneSecondaryPercent")
        value = int(cmds.optionVar(q=key))
        return min(100, max(0, ((value + 2) // 5) * 5))

    def _set_edge_bias(self, percent, bevel=False):
        percent = min(100, max(0, ((int(percent) + 2) // 5) * 5))
        key = ("ModelPack_BevelSecondaryPercent" if bevel else
               "ModelPack_CloneSecondaryPercent")
        cmds.optionVar(intValue=(key, percent))
        slide_ui.set_edge_distribution(percent, bevel=bevel)
        return percent

    def _add_edge_count(self, menu, bevel=False):
        menu.addSeparator()
        action = QtWidgets.QWidgetAction(menu)
        widget = QtWidgets.QWidget(menu)
        layout = QtWidgets.QGridLayout(widget)
        layout.setContentsMargins(12, 4, 12, 4)
        layout.addWidget(QtWidgets.QLabel("Количество рёбер", widget), 0, 0)
        spin = QtWidgets.QSpinBox(widget)
        spin.setRange(1, 16)
        spin.setValue(self._edge_count(bevel))
        layout.addWidget(spin, 0, 1)
        label = QtWidgets.QLabel("Положение доп. рёбер", widget)
        label.setToolTip("50% — равные интервалы; меньше — к исходному ребру, больше — к ведущему.\n"
                         "На 0% и 100% промежуточные рёбра объединяются с краем.")
        layout.addWidget(label, 1, 0)
        bias_spin = QtWidgets.QSpinBox(widget)
        bias_spin.setRange(0, 100)
        bias_spin.setSingleStep(5)
        bias_spin.setKeyboardTracking(False)
        bias_spin.setSuffix("%")
        bias_spin.setValue(self._edge_bias(bevel))
        bias_spin.setEnabled(spin.value() > 1)
        bias_spin.setToolTip(label.toolTip())
        layout.addWidget(bias_spin, 1, 1)
        spin.valueChanged.connect(lambda value: self._set_edge_count(value, bevel))
        spin.valueChanged.connect(lambda value: bias_spin.setEnabled(value > 1))
        def set_bias(value):
            snapped = self._set_edge_bias(value, bevel)
            if bias_spin.value() != snapped:
                bias_spin.blockSignals(True)
                bias_spin.setValue(snapped)
                bias_spin.blockSignals(False)
        bias_spin.valueChanged.connect(set_bias)
        action.setDefaultWidget(widget)
        menu.addAction(action)

    def tool_slide(self):
        self._prepare_tool("clone")
        if slide_ui.active():
            slide_ui.deactivate_drag()
            self._close_edit_panel();self._active_tool=None
        else:
            slide_ui.set_edge_distribution(self._edge_bias())
            slide_ui.activate_drag(mode=self._clone_mode(), count=self._edge_count())
            self._show_edit_panel('clone')
        self._sync_slide_button()

    def _sync_slide_button(self):
        context = cmds.currentCtx()
        if (context!=self._last_context and self._active_tool is None and not self._changing_tool
                and gesture_bevel_ui._TOOLBOX is self):
            gesture_bevel_ui._hide_values()
        self._last_context = context
        if not self._changing_tool:
            if slide_ui._SESSION is not None and context!=slide_ui._SESSION['context']:
                slide_ui._on_finalize()
            if self._active_tool=='linear_shift' and context!=linear_shift_ui.CONTEXT and linear_shift_ui._FILTER is not None:
                linear_shift_ui.shutdown()
            allowed = {"bevel":(gesture_bevel_ui.CONTEXT,),"clone":(slide_ui._CONTEXT,),
                       "slide_bevel":(slide_ui._BEVEL_CONTEXT,),
                       "unbevel":("selectSuperContext",unbevel_ui.CONTEXT),
                       "selection":("selectSuperContext",selection_ui.CONTEXT),
                       "hard_edges":("selectSuperContext",),
                       "linear_shift":(linear_shift_ui.CONTEXT,)}
            if self._active_tool not in EDITABLE and self._active_tool in allowed and cmds.currentCtx() not in allowed[self._active_tool]:
                self._prepare_tool(None)
        if hasattr(self,"selection_button"):
            self.selection_button.setChecked(not self.selection_panel.isHidden())
            self.selection_actions["Область"].setChecked(selection_ui.active())
        if hasattr(self,"linear_shift_button"):
            self.linear_shift_button.setChecked(linear_shift_ui.active() or self._edit_kind=='linear_shift')
        if hasattr(self,'hard_edge_panel'):
            self.hard_edge_button.setChecked(not self.hard_edge_panel.isHidden())
        if hasattr(self, "unbevel_button"):
            self.unbevel_button.setChecked(not self.unbevel_panel.isHidden())
        if hasattr(self, "gesture_bevel_button"):
            self.gesture_bevel_button.setChecked(gesture_bevel_ui.active())
        try:
            is_active = slide_ui.active()
            bevel_active = slide_ui.active_bevel()
        except Exception:
            is_active = False
            bevel_active = False
        if self._edit_panel is not None and hasattr(self._edit_panel,'resume'):
            active={'clone':is_active,'slide_bevel':bevel_active,'linear_shift':linear_shift_ui.active()}
            self._edit_panel.resume.setEnabled(not active.get(self._edit_kind,False))
        self.slide_button.setChecked(is_active or self._edit_kind=='clone')
        icon = "slide_active.png" if is_active else "slide_inactive.png"
        self.slide_button.setIcon(QtGui.QIcon(os.path.join(self.icon_path, icon)))
        self.bevel_button.setChecked(bevel_active or self._edit_kind=='slide_bevel')
        bevel_icon = "slide_bevel_on.png" if bevel_active else "slide_bevel_off.png"
        self.bevel_button.setIcon(QtGui.QIcon(os.path.join(self.icon_path, bevel_icon)))
        for button in self.findChildren(ModelPackButton):
            kind={'Mirror Cut':'mirror_cut','Mirror Dup':'mirror_dup','Reset Tube':'reset_tube'}.get(button.text())
            if kind is not None:button.setChecked(self._edit_kind==kind)

    def _set_clone_mode(self, mode):
        cmds.optionVar(intValue=("ModelPack_CloneOffsetMode", 0 if mode == "relative" else 1))
        if slide_ui.active():
            slide_ui.activate_drag(mode=mode)

    def show_slide_context_menu(self, pos):
        sender = self.sender()
        menu = QtWidgets.QMenu(self)
        group = UniversalActionGroup(menu)
        for label, mode in (("Пропорционально", "relative"),
                            ("Равная ширина", "uniform")):
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(self._clone_mode() == mode)
            group.addAction(action)
            action.triggered.connect(lambda checked=False, m=mode: self._set_clone_mode(m))
        self._add_edge_count(menu)
        menu.exec_(sender.mapToGlobal(pos))

    def _bevel_mode(self):
        return ("relative", "uniform")[int(cmds.optionVar(q="ModelPack_BevelOffsetMode"))]

    def tool_slide_bevel(self):
        self._prepare_tool("slide_bevel")
        if slide_ui.active_bevel():
            slide_ui.deactivate_bevel_drag()
            self._close_edit_panel();self._active_tool=None
        else:
            slide_ui.set_edge_distribution(self._edge_bias(True), bevel=True)
            slide_ui.activate_bevel_drag(mode=self._bevel_mode(), count=self._edge_count(True))
            self._show_edit_panel('slide_bevel')
        self._sync_slide_button()

    def tool_linear_shift(self):
        self._prepare_tool("linear_shift")
        if linear_shift_ui.activate():self._show_edit_panel('linear_shift')
        else:self._close_edit_panel();self._active_tool=None
        self._sync_slide_button()

    def _linear_shift_menu(self):
        menu=QtWidgets.QMenu(self);group=UniversalActionGroup(menu)
        menu._frame_group=group
        for label,mode in (("Локально","local"),("Глобально","global"),("Мировые","world")):
            action=menu.addAction(label);action.setCheckable(True)
            action.setChecked(linear_shift_ui.frame_mode()==mode);group.addAction(action)
            action.triggered.connect(lambda checked=False,value=mode:linear_shift_ui.set_frame_mode(value))
        row=QtWidgets.QWidget(menu);layout=QtWidgets.QHBoxLayout(row)
        layout.setContentsMargins(6,4,6,4);layout.setSpacing(4)
        axis_group=QtWidgets.QButtonGroup(row);axis_group.setExclusive(True)
        row._axis_group=axis_group
        selected_axis=linear_shift_ui.plane_axis()
        def choose_axis(axis):
            linear_shift_ui.set_plane_axis(axis);menu.close()
        for axis in ('x','y','z'):
            button=QtWidgets.QPushButton(axis.upper(),row);button.setCheckable(True)
            button.setObjectName('planeSlideAxis'+axis.upper())
            button.setToolTip('Нормаль плоскости: '+axis.upper())
            button.setMinimumSize(36,24)
            button.setStyleSheet('QPushButton:checked { background-color: #467c95; color: white; }')
            button.setChecked(selected_axis==axis);button.setEnabled(selected_axis is not None)
            axis_group.addButton(button);layout.addWidget(button)
            button.clicked.connect(lambda checked=False,value=axis:choose_axis(value))
        action=QtWidgets.QWidgetAction(menu);action.setDefaultWidget(row);menu.addAction(action)
        return menu

    def show_linear_shift_menu(self,pos):
        sender=self.sender() or self.linear_shift_button
        self._linear_shift_menu().exec_(sender.mapToGlobal(pos))

    def tool_gesture_bevel(self):
        self._prepare_tool("bevel")
        gesture_bevel_ui.activate(self)
        self._sync_slide_button()

    def tool_unbevel(self):
        self._prepare_tool("unbevel")
        showing = self.unbevel_panel.isHidden()
        if showing:
            if cmds.currentCtx()!='selectSuperContext':cmds.setToolTo("selectSuperContext")
            self._unbevel_compact_size = QtCore.QSize(self.size())
        else:
            unbevel_ui.shutdown()
            self._active_tool = None
        self.unbevel_panel.setVisible(showing)
        self.layout().activate()
        if showing:
            self.resize(self.width(), max(self.height(), self.sizeHint().height()))
        elif self._unbevel_compact_size is not None:
            self.resize(self._unbevel_compact_size)
            self._unbevel_compact_size = None
        self._sync_slide_button()

    def tool_selection(self):
        self._prepare_tool("selection")
        showing = self.selection_panel.isHidden()
        if showing:
            cmds.setToolTo("selectSuperContext")
            self._selection_compact_size = QtCore.QSize(self.size())
        else:
            selection_ui.shutdown()
            self._active_tool = None
        self.selection_panel.setVisible(showing);self.layout().activate()
        if showing:self.resize(self.width(),max(self.height(),self.sizeHint().height()))
        elif self._selection_compact_size is not None:
            self.resize(self._selection_compact_size);self._selection_compact_size = None
        self._sync_slide_button()

    def tool_selection_region(self):
        self._prepare_tool("selection")
        result = selection_ui.activate_region();self._sync_slide_button()
        return result

    def tool_selection_grow(self,shrink=False):
        self._prepare_tool("selection")
        result = selection_ui.grow(shrink=shrink);self._sync_slide_button()
        return result

    def tool_selection_periodic(self,across=False):
        self._prepare_tool("selection")
        result = selection_ui.periodic(across=across,skip=int(cmds.optionVar(q="ModelPack_SelectionSkip")))
        self._sync_slide_button()
        return result

    def _selection_menu(self):
        menu = QtWidgets.QMenu(self)
        for label,command in (("Выделение области",self.tool_selection_region),
                              ("Спец Grow",lambda:self.tool_selection_grow()),
                              ("Спец Shrink",lambda:self.tool_selection_grow(shrink=True)),
                              ("Вдоль кольца",lambda:self.tool_selection_periodic()),
                              ("Поперёк кольца",lambda:self.tool_selection_periodic(across=True))):
            menu.addAction(label).triggered.connect(lambda checked=False,action=command:action())
        return menu

    def show_selection_menu(self,pos):
        sender = self.sender() or self.selection_button
        self._selection_menu().exec_(sender.mapToGlobal(pos))

    def tool_remove_bevels(self):
        self._prepare_tool("unbevel")
        targets = {_mesh_fn(item.split('.')[0]).fullPathName() for item in cmds.ls(selection=True,long=True) or []
                   if '.f[' in item or '.e[' in item}
        known = set()
        for mesh,identity in self._bevel_group_topology.items():
            try:
                if self._bevel_mesh_identity(mesh)==identity:known.add(mesh)
            except (ValueError,RuntimeError):continue
        previous = self._bevel_groups
        self._remember_bevel_groups()
        face_maps = {}
        result = unbevel_ui.remove_selected(face_maps=face_maps)
        if result:
            try:
                groups = []
                data = {}
                for group in previous:
                    if group.mesh not in known:continue
                    if group.mesh not in targets:
                        groups.append(group);continue
                    if group.mesh not in known or group.mesh not in face_maps:continue
                    mapping = face_maps[group.mesh]
                    faces = {mapping[f] for f in group.faces if f in mapping}
                    if not faces:continue
                    if group.mesh not in data:data[group.mesh] = bevel_detection._Mesh(group.mesh)
                    groups.append(bevel_detection._group(data[group.mesh],faces,group.source))
                for mesh in sorted(targets-known):
                    groups.extend(g for g in bevel_detection.detect(mesh).groups
                                  if g.ready or not cmds.optionVar(q="ModelPack_FindBevelReadyOnly"))
                self._set_bevel_groups(groups, selected=False)
                self._remember_bevel_groups()
            except (ValueError,RuntimeError):self._set_bevel_groups(())
        self._sync_slide_button()
        return result

    def tool_find_bevels(self, seeded=None):
        self._prepare_tool("unbevel")
        ready_only=bool(cmds.optionVar(q="ModelPack_FindBevelReadyOnly"))
        def display(result):
            chosen=tuple(g for g in result if g.ready or not ready_only)
            self._bevel_group_states.clear()
            self._set_bevel_groups(chosen)
            self._remember_bevel_groups()
            self._sync_slide_button()
        result = bevel_detection.select_bevels(
            seeded=seeded,
            sensitivity=("strict", "normal", "extended")[max(0, min(2, int(
                cmds.optionVar(q="ModelPack_FindBevelSensitivity"))))],
            include_corners=bool(cmds.optionVar(q="ModelPack_FindBevelCorners")),
            ready_only=ready_only,_on_result=display)
        return result

    def _set_bevel_groups(self, groups, selected=True,open_panel=True):
        groups=tuple(groups)
        topology={mesh:self._bevel_mesh_identity(mesh) for mesh in {g.mesh for g in groups}}
        if groups==self._bevel_groups:
            self._bevel_group_topology=topology
            self.bevel_groups_list.blockSignals(True)
            try:
                for index in range(self.bevel_groups_list.count()):self.bevel_groups_list.item(index).setSelected(selected)
            finally:self.bevel_groups_list.blockSignals(False)
            if groups and open_panel and self.unbevel_panel.isHidden():self.tool_unbevel()
            return
        previous_hint = self.sizeHint().height()
        self._bevel_groups = tuple(groups)
        self._bevel_group_topology = topology
        self.bevel_groups_list.blockSignals(True)
        try:
            self.bevel_groups_list.clear()
            for index, group in enumerate(groups):
                parent = (cmds.listRelatives(group.mesh, parent=True, fullPath=True) or [group.mesh])[0]
                label = f"Фаска {index+1:02d} · {len(group.faces)} полигонов"
                if len(self._bevel_group_topology)>1:label = parent.rsplit('|',1)[-1]+" · "+label
                if not group.ready:label += " · проверить"
                item = QtWidgets.QListWidgetItem(label)
                item.setData(QtCore.Qt.UserRole, index)
                item.setToolTip(parent+"\n"+(group.reason or "Можно удалить через UnBevel."))
                self.bevel_groups_list.addItem(item)
                item.setSelected(selected)
        finally:self.bevel_groups_list.blockSignals(False)
        self.bevel_groups_label.setText(f"Найденные фаски: {len(groups)}")
        self.bevel_groups_label.setVisible(bool(groups))
        self.bevel_groups_list.setVisible(bool(groups))
        if groups and open_panel and self.unbevel_panel.isHidden():self.tool_unbevel()
        self.layout().activate()
        if groups and not self.unbevel_panel.isHidden():self.resize(self.width(),max(self.height(),self.sizeHint().height()))
        elif not self.unbevel_panel.isHidden():
            hint = self.sizeHint().height()
            self.resize(self.width(),max(hint,self.height()-max(previous_hint-hint,0)))

    @staticmethod
    def _bevel_mesh_identity(mesh):
        from model_pack_bevel.search_cache import topology_identity
        return topology_identity(mesh)

    def _remember_bevel_groups(self):
        # Include meshes whose last group was removed, so Undo can restore
        # their rows even when the current list is empty.
        meshes = set(self._bevel_group_topology)
        if self._bevel_group_states:meshes.update(self._bevel_group_states[-1][0])
        try:
            identities = {m:self._bevel_mesh_identity(m) for m in meshes if cmds.objExists(m)}
            if any(identities.get(m)!=identity for m,identity in self._bevel_group_topology.items()):return
        except (ValueError,RuntimeError):return
        state = (identities,self._bevel_groups)
        if not self._bevel_group_states or state != self._bevel_group_states[-1]:
            self._bevel_group_states.append(state)
            self._bevel_group_states = self._bevel_group_states[-16:]

    def _restore_bevel_groups(self, *args):
        current={}
        for identities,groups in reversed(self._bevel_group_states):
            try:
                for mesh in identities:
                    if mesh not in current:current[mesh]=self._bevel_mesh_identity(mesh)
                if all(current[m]==identity for m,identity in identities.items()):
                    if groups != self._bevel_groups:self._set_bevel_groups(groups,selected=False,open_panel=False)
                    return
            except (ValueError,RuntimeError):continue

    def _select_bevel_groups(self):
        groups = [self._bevel_groups[item.data(QtCore.Qt.UserRole)] for item in self.bevel_groups_list.selectedItems()]
        try:
            cmds.setToolTo("selectSuperContext")
            # Cached indices must never select unrelated faces after a merge,
            # external edit or deletion of the object.
            for group in groups:
                if self._bevel_mesh_identity(group.mesh) != self._bevel_group_topology[group.mesh]:
                    raise ValueError("Сетка изменилась. Нажмите «Найти фаски» для обновления списка.")
            cmds.undoInfo(openChunk=True,chunkName="Select bevel group")
            try:
                if groups:
                    cmds.selectMode(component=True)
                    cmds.selectType(allComponents=False);cmds.selectType(polymeshFace=True)
                    parents = list({(cmds.listRelatives(g.mesh,parent=True,fullPath=True) or [g.mesh])[0] for g in groups})
                    cmds.hilite(parents,replace=True)
                    cmds.select([g.mesh+f".f[{f}]" for g in groups for f in g.faces],replace=True)
                else:cmds.select(clear=True)
            finally:cmds.undoInfo(closeChunk=True)
        except (ValueError,RuntimeError) as error:
            self._set_bevel_groups(())
            cmds.warning("UnBevel: "+str(error))

    def _unbevel_menu(self):
        menu = QtWidgets.QMenu(self)
        menu.addAction("Найти фаски в выбранных мешах").triggered.connect(
            lambda checked=False: self.tool_find_bevels(seeded=False))
        menu.addAction("Расширить от выделенных полигонов фаски").triggered.connect(
            lambda checked=False: self.tool_find_bevels(seeded=True))
        menu.addSeparator()
        sensitivity = menu.addMenu("Чувствительность поиска")
        group = UniversalActionGroup(sensitivity)
        for index, label in enumerate(("Строгая", "Обычная", "Расширенная")):
            action = sensitivity.addAction(label)
            action.setCheckable(True)
            action.setChecked(cmds.optionVar(q="ModelPack_FindBevelSensitivity") == index)
            group.addAction(action)
            action.triggered.connect(lambda checked=False, value=index: cmds.optionVar(
                intValue=("ModelPack_FindBevelSensitivity", value)))
        for label, key in (("Включать угловые стыки", "ModelPack_FindBevelCorners"),
                           ("Только подтверждённые для UnBevel", "ModelPack_FindBevelReadyOnly")):
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(bool(cmds.optionVar(q=key)))
            action.triggered.connect(lambda checked, name=key: cmds.optionVar(intValue=(name, int(checked))))
        return menu

    def show_unbevel_menu(self, pos):
        sender = self.sender() or self.find_bevel_button
        self._unbevel_menu().exec_(sender.mapToGlobal(pos))

    def show_gesture_bevel_menu(self, pos):
        sender = self.sender() or self.gesture_bevel_button
        gesture_bevel_ui.show_menu(self, sender.mapToGlobal(pos))

    def closeEvent(self, event):
        self.slide_timer.stop()
        self._clear_hard_edges()
        self._close_edit_panel()
        slide_ui.deactivate_drag();slide_ui.deactivate_bevel_drag()
        selection_ui.shutdown()
        linear_shift_ui.shutdown()
        for callback in self._bevel_group_callbacks:om.MMessage.removeCallback(callback)
        self._bevel_group_callbacks = []
        unbevel_ui.shutdown()
        if gesture_bevel_ui._TOOLBOX is self:
            gesture_bevel_ui.shutdown()
        super().closeEvent(event)

    def _set_bevel_mode(self, mode):
        cmds.optionVar(intValue=("ModelPack_BevelOffsetMode", 0 if mode == "relative" else 1))
        if slide_ui.active_bevel():
            slide_ui.activate_bevel_drag(mode=mode)

    def show_bevel_context_menu(self, pos):
        sender = self.sender()
        menu = QtWidgets.QMenu(self)
        group = UniversalActionGroup(menu)
        for label, mode in (("Пропорционально", "relative"),
                            ("Равная ширина", "uniform")):
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(self._bevel_mode() == mode)
            group.addAction(action)
            action.triggered.connect(lambda checked=False, m=mode: self._set_bevel_mode(m))
        self._add_edge_count(menu, bevel=True)
        menu.exec_(sender.mapToGlobal(pos))

    # ------------------------------------------------------------------ #
    # HARD EDGES — live angle selection and Maya Harden Edge
    # ------------------------------------------------------------------ #
    def tool_hard_edges(self):
        self._prepare_tool('hard_edges')
        if not self.hard_edge_panel.isHidden():
            self._clear_hard_edges();self._active_tool=None;self._sync_slide_button();return
        try:
            if cmds.currentCtx()!='selectSuperContext':cmds.setToolTo('selectSuperContext')
            self._hard_edge_session=hard_edges.Selection(hard_edges.selected_meshes())
            cmds.undoInfo(openChunk=True,chunkName='Hard Edges')
            try:count=self._hard_edge_session.apply(self.hard_edge_slider.value()/10,configure=True)
            finally:cmds.undoInfo(closeChunk=True)
        except (ValueError,RuntimeError) as error:
            self._clear_hard_edges();self._active_tool=None;self._sync_slide_button()
            cmds.warning('Hard Edges: '+str(error));return False
        self.hard_edge_count_label.setText(f'Рёбер: {count}')
        self._hard_edge_compact_size=QtCore.QSize(self.size())
        self.hard_edge_panel.show();self.layout().activate()
        self.resize(self.width(),max(self.height(),self.sizeHint().height()))
        self._sync_slide_button();return True

    def _hard_edge_begin_drag(self):
        if self._hard_edge_session is not None and not self._hard_edge_chunk:
            cmds.undoInfo(openChunk=True,chunkName='Hard Edges angle')
            self._hard_edge_chunk=True

    def _hard_edge_finish_drag(self):
        if self._hard_edge_chunk:
            self._hard_edge_chunk=False;cmds.undoInfo(closeChunk=True)

    def _clear_hard_edges(self):
        self._hard_edge_finish_drag()
        if self._hard_edge_session is not None:
            self._hard_edge_session.close();self._hard_edge_session=None
        if not self.hard_edge_panel.isHidden():
            self.hard_edge_panel.hide();self.layout().activate()
            if self._hard_edge_compact_size is not None:self.resize(self._hard_edge_compact_size)
        self._hard_edge_compact_size=None

    def _hard_edge_angle_changed(self,value):
        angle=value/10
        cmds.optionVar(floatValue=('ModelPack_HardEdgeAngle',angle))
        self.hard_edge_angle_label.setText(f'Угол ≥ {angle:.1f}°')
        if self._hard_edge_session is None or self.hard_edge_panel.isHidden():return
        try:
            count=self._hard_edge_session.apply(angle)
            self.hard_edge_count_label.setText(f'Рёбер: {count}')
        except (ValueError,RuntimeError) as error:
            self._clear_hard_edges();self._active_tool=None;self._sync_slide_button()
            cmds.warning('Hard Edges: '+str(error))

    def tool_harden_edges(self):
        self._hard_edge_finish_drag()
        try:
            count=hard_edges.harden_selected()
            self.hard_edge_count_label.setText(f'Жёстких рёбер: {count}')
            return True
        except (ValueError,RuntimeError) as error:
            cmds.warning('Harden Edge: '+str(error));return False

    # ------------------------------------------------------------------ #
    # RESECT TUBE  (tube cross-section resampler)
    # ------------------------------------------------------------------ #
    def tool_resect_tube(self):
        return self._activate_modifier('reset_tube')


def launch_toolbox():
    # Old windows call this helper too. Upgrade teardown before closing them.
    import model_pack_mirror_session
    importlib.reload(model_pack_mirror_session)
    if cmds.workspaceControl(WORKSPACE_WINDOW_NAME, exists=True):
        cmds.deleteUI(WORKSPACE_WINDOW_NAME)
    elif cmds.window(WORKSPACE_WINDOW_NAME, exists=True):
        cmds.deleteUI(WORKSPACE_WINDOW_NAME)
    for widget in QtWidgets.QApplication.topLevelWidgets():
        if widget.objectName() == WORKSPACE_WINDOW_NAME:
            widget.close()
            widget.deleteLater()
    import model_pack_mirror_session
    model_pack_mirror_session.finish_all()
    # This is also the documented direct entry point. Reload face recognition
    # here so re-running the toolbox does not retain old UnBevel warnings.
    unbevel_ui.shutdown()
    selection_ui.shutdown()
    linear_shift_ui.shutdown()
    for name in ("model_pack_bevel.unbevel_regions","model_pack_bevel.unbevel_triangles","model_pack_bevel.unbevel_faces","model_pack_bevel.search_cache","model_pack_bevel.unbevel","model_pack_bevel.bevel_detection","model_pack_bevel.seeded_detection",
                 "model_pack_selection.core","model_pack_selection.ui","model_pack_hard_edges",
                 "model_pack_paths","model_pack_mirror_geometry","model_pack_legacy_mesh","model_pack_mirror_plane","model_pack_mirror_snap","model_pack_mirror_viewport","model_pack_modifiers","model_pack_mirror_session","model_pack_tool_panels",
                 "model_pack_linear_shift.core","model_pack_linear_shift.components","model_pack_linear_shift.overlay","model_pack_linear_shift.ui"):
        if name in sys.modules:
            importlib.reload(sys.modules[name])
    global model_pack_ui
    global ToolPanels
    ToolPanels=sys.modules['model_pack_tool_panels'].ToolPanels
    # Existing instances are closed above; refresh the mixin used by new ones.
    for name,method in vars(ToolPanels).items():
        if callable(method):setattr(ModelPackToolbox,name,method)
    import model_pack_modifiers
    model_pack_modifiers.refresh_plugin()
    # Recover connections left behind by earlier versions after window close.
    cmds.undoInfo(openChunk=True,chunkName='Finish previous Mirror Cut')
    try:model_pack_modifiers.freeze_mirror_frame(cmds.ls(type='modelPackMirrorCut') or [])
    finally:cmds.undoInfo(closeChunk=True)
    model_pack_ui = ModelPackToolbox()
    model_pack_ui.show()


if __name__ == "__main__":
    launch_toolbox()
