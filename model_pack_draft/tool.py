"""Two-gesture viewport drawing context with a transient API preview."""

import sys
import math
import traceback
from maya import cmds
from maya.api import OpenMaya as om
from maya.api import OpenMayaUI as omui

from . import core
from . import snapping
from . import hotkey_snaps
from . import patterns
from .preview import Preview


CONTEXT = "modelPackFastDraftContext"
_active = None


def start(shape="box", mode="auto", segments=32, angle=0.0, sensitivity=1.0,
          keep_history=True, selected_only=False):
    global _active
    stop()
    factory = LineSketch if shape == "line" else SurfaceTool
    _active = factory(shape, mode, segments, angle, sensitivity, keep_history, selected_only)
    _active.activate()
    return _active


def stop(preserve=True):
    global _active
    if _active:
        _active.cancel(preserve=preserve)
        if cmds.currentCtx() == CONTEXT:
            cmds.setToolTo("selectSuperContext")
        _active = None
    if cmds.draggerContext(CONTEXT, exists=True):
        cmds.deleteUI(CONTEXT, toolContext=True)


def configure(changed=None, **settings):
    """Apply UI changes to the existing context without replacing its callbacks."""
    if _active is None:
        return False
    if isinstance(_active, StampTool) and changed in ("shape", "mode", "segments"):
        start(**settings)
        return True
    _active.configure(**settings)
    return True


def start_copy(template=None, angle=0.0, keep_history=True, selected_only=False):
    global _active
    from . import cuts
    if template is None:
        template = cuts.from_selection()
    stop()
    _active = StampTool(template, angle, keep_history, selected_only)
    _active.activate()
    return _active


class SurfaceTool:
    def __init__(self, shape, mode, segments, angle, sensitivity, keep_history, selected_only):
        self.shape = shape
        self.mode = mode
        self.segments = segments
        self.angle = angle
        self.sensitivity = sensitivity
        self.keep_history = keep_history
        self.selected_only = selected_only
        self.preview = Preview()
        self.phase = "ready"
        self.footprint = None
        self.completed_base = False
        self.pattern_uuid = None
        self.needs_depth_scale = False
        self.target = None
        self.view = None
        self.panel = None
        self.dragging = False
        self.depth = 0.0
        self.units_per_pixel = 1.0
        self.press_position = (0.0, 0.0)
        self.depth_direction = (0.0, 1.0)
        self.depth_scale = 1.0
        self.depth_fallback = False
        self.candidates = []
        self.selection_roots = cmds.ls(selection=True, objectsOnly=True, long=True) or []
        self.snap_enabled=True
        # The host owns momentary V/C/X keys. Maya's global snapMode flags can
        # remain enabled after Modeling Toolkit release commands have run.
        self.snap_mode=None
        self.native_snaps=hotkey_snaps.Resolver()

    def native_snap(self,kind,position,hit=None,frame=None,proposed=None):
        if frame is None and hit is not None:frame=core.surface_frame(hit,self.angle)
        if proposed is None and hit is not None:proposed=hit.point
        curves=self.collect_paths('nurbsCurve','nurbsCurves') if kind=='curve' and self.native_snaps.curves is None else ()
        snap=self.native_snaps.find(kind,self.view,position,self.candidates,frame,proposed,curves)
        if snap is not None and hit is not None and kind in ('grid','curve') and not hotkey_snaps.on_surface(snap,hit):return None
        return snap

    def set_snapping(self,enabled):
        self.snap_enabled=bool(enabled)
        if not self.snap_enabled:self.preview.set_snap(None,self.view)

    def hover(self,view,panel,position,refresh_meshes=False):
        if self.phase!='ready' or self.dragging:return
        changed=self.panel!=panel
        self.view,self.panel=view,panel
        kind=self.snap_mode
        if not self.snap_enabled and kind is None:
            self.preview.set_snap(None,view);return
        if changed or refresh_meshes or not self.candidates:
            self.candidates=self.collect_meshes();self.native_snaps.clear()
        hit=core.raycast(*self.ray(*position),self.candidates) if self.candidates else None
        snap=(self.native_snap(kind,position,hit) if kind else
              snapping.find(hit,view,position,self.candidates) if hit is not None else None)
        self.preview.set_snap(snap,view)

    def activate(self):
        cmds.draggerContext(CONTEXT, pressCommand=self.press, dragCommand=self.drag,
                           releaseCommand=self.release, finalize=self.finalize,
                           cursor="crossHair", space="screen", undoMode="all")
        cmds.setToolTo(CONTEXT)
        self.message("Draw on a mesh; then drag away from its surface to add, into it to cut. Q exits.")

    def configure(self, **settings):
        allowed = {"shape", "mode", "segments", "angle", "sensitivity", "keep_history", "selected_only"}
        if set(settings) - allowed:
            raise ValueError("Unknown Fast Draft settings.")
        changed = any(getattr(self, key) != value for key, value in settings.items())
        if not changed:
            return
        # A footprint's topology and basis belong to the old settings; discard
        # only that unfinished gesture, keeping all committed geometry intact.
        self.cancel(preserve=True)
        previous_selected_only = self.selected_only
        for key, value in settings.items():
            setattr(self, key, value)
        if self.selected_only and not previous_selected_only:
            self.selection_roots = cmds.ls(selection=True, objectsOnly=True, long=True) or []
        self.message("{} selected. Draw a new footprint on the surface axes.".format(
            "Cylinder" if self.shape == "cylinder" else "Box"))

    def set_sections(self, number):
        """Update the active footprint in place, preserving both gestures."""
        if self.shape != "cylinder" or getattr(self.footprint,'from_curve',False):return
        self.segments = max(3, min(256, int(number)))
        if self.footprint is not None:self.footprint.segments = self.segments
        if self.footprint is not None and self.footprint.valid:
            depth = self.depth if abs(self.depth) > core.EPSILON else self.units_per_pixel * 0.1
            self.preview.update(self.footprint, depth, self.view)
            cmds.refresh(currentView=True, force=True)

    def message(self, text):
        from model_pack_bevel.session import without_undo
        with without_undo():cmds.draggerContext(CONTEXT, edit=True, drawString=text)

    def target_name(self):
        if self.target is None or not self.target.isValid() or not self.target.isAlive():return None
        paths=om.MDagPath.getAllPathsTo(self.target.object())
        return paths[0].fullPathName() if paths else None

    def save_pattern(self,select=True):
        if not self.completed_base or self.footprint is None or not self.footprint.valid:return None
        self.pattern_uuid=patterns.save(self.footprint,self.target_name(),self.pattern_uuid,select=select)
        return self.pattern_uuid

    def resume(self,uuid,target=None):
        view=omui.M3dView.active3dView()
        normal=None
        try:normal=-om.MFnCamera(view.getCamera()).viewDirection(om.MSpace.kWorld)
        except (AttributeError,RuntimeError,TypeError):pass
        footprint,target=patterns.read(uuid,mesh=target,normal=normal)
        self.cancel(preserve=True)
        self.footprint=footprint;self.pattern_uuid=uuid;self.completed_base=True
        patterns.choose(uuid)
        if footprint.shape in ('box','cylinder'):
            self.shape=footprint.shape;self.segments=footprint.segments
        self.target=om.MObjectHandle(core.path_for(target).node()) if target else None
        self.view=view
        self.needs_depth_scale=True
        self.phase='depth_ready'
        self.preview.update(footprint,.001,self.view)
        self.message('Open curve: drag to extrude a surface. Q/W/E/R: edit the curve in Maya.' if not footprint.closed else
                     'Closed curve: drag away to add, into the mesh to cut. Select curve + mesh or drag on the target.')

    def finalize(self,*_):
        self.cancel(preserve=True)

    def cancel(self, *_, preserve=False):
        if preserve:self.save_pattern()
        self.preview.clear()
        self.native_snaps.clear()
        self.phase = "ready"
        self.dragging = False
        self.footprint = None
        self.completed_base = False
        self.pattern_uuid = None
        self.target = None
        self.depth = 0.0

    def fail(self):
        error = str(sys.exc_info()[1])
        traceback.print_exc()
        self.cancel()
        self.message("Error: " + error)
        cmds.warning("Fast Draft: " + error)

    def position(self, anchor=False):
        flag = {"anchorPoint" if anchor else "dragPoint": True}
        return cmds.draggerContext(CONTEXT, query=True, **flag)[:2]

    def ray(self, x, y):
        origin, direction = om.MPoint(), om.MVector()
        self.view.viewToWorld(int(x), int(y), origin, direction)
        return origin, direction.normal()

    def collect_meshes(self):
        """Enumerate actual DAG meshes, then apply viewport isolation.

        filteredObjectList is a view-filter result, not a reliable scene mesh
        enumeration in Viewport 2.0; it can be empty with visible geometry.
        """
        return self.collect_paths('mesh','polymeshes')

    def collect_paths(self,kind,display):
        if self.panel and not cmds.modelEditor(self.panel, query=True, **{display:True}):
            return []
        isolation = None
        if self.panel and cmds.isolateSelect(self.panel, query=True, state=True):
            object_set = cmds.isolateSelect(self.panel, query=True, viewObjects=True)
            members = cmds.sets(object_set, query=True) or []
            isolation = set()
            for member in members:
                root = member.split(".", 1)[0]
                isolation.update(cmds.ls(root, long=True) or [])
                isolation.update(cmds.ls(root, dag=True, type=kind, long=True) or [])
        paths = []
        for name in cmds.ls(dag=True, allPaths=True, type=kind, long=True,
                            noIntermediate=True, visible=True) or []:
            path = core.path_for(name)
            if not path.isVisible():
                continue
            name = path.fullPathName()
            if isolation is not None and name not in isolation:
                continue
            if self.selected_only and not any(name == root or name.startswith(root + "|")
                                              for root in self.selection_roots):
                continue
            paths.append(path)
        return paths

    def press(self, *_):
        try:
            if cmds.draggerContext(CONTEXT, query=True, button=True) != 1:
                self.cancel()
                return
            if cmds.getModifiers() & 8:  # Let Maya handle Alt camera navigation.
                return
            self.press_position = self.position(anchor=True)
            self.dragging = False
            if self.phase == "ready":
                panel = cmds.getPanel(underPointer=True)
                self.panel = panel if panel and cmds.getPanel(typeOf=panel) == "modelPanel" else None
                self.view = (omui.M3dView.getM3dViewFromModelPanel(self.panel) if self.panel
                             else omui.M3dView.active3dView())
                self.candidates = self.collect_meshes()
                self.native_snaps.clear()
                if not self.candidates:
                    self.message("No target meshes. Create/unhide a mesh or disable selection-only mode.")
                    cmds.warning("Fast Draft: no visible polygon mesh targets in this viewport.")
                    return
                hit = core.raycast(*self.ray(*self.press_position), self.candidates)
                kind=self.snap_mode
                snap=self.native_snap(kind,self.press_position,hit) if kind else None
                if snap is not None and kind=='point':
                    hit=hotkey_snaps.vertex_hit(snap,self.view,self.press_position,hit)
                if hit is None:
                    self.preview.set_snap(None,self.view)
                    self.message("No visible polygon mesh under the pointer.")
                    cmds.warning("Fast Draft: click directly on a visible polygon mesh surface.")
                    return
                if kind is None and self.snap_enabled:snap=snapping.find(hit,self.view,self.press_position,self.candidates)
                self.preview.set_snap(snap,self.view)
                self.target = om.MObjectHandle(hit.path.node())
                self.target_path = hit.path.fullPathName()
                frame = core.surface_frame(hit, self.angle)
                if snap is not None:frame.origin=om.MPoint(snap.point)
                self.footprint = core.Footprint(frame, self.shape, 0, 0, self.segments)
                # Screen-space depth scale at the picked world-space point.
                origin, direction = self.ray(*self.press_position)
                offset_origin, offset_direction = self.ray(self.press_position[0] + 1,
                                                         self.press_position[1])
                adjacent = core.intersect_plane(offset_origin, offset_direction, frame.origin, direction)
                base = core.intersect_plane(origin,direction,frame.origin,direction)
                self.units_per_pixel = max((adjacent-base).length() if adjacent is not None and base is not None else 0.01, 1e-6)
                self.phase = "base_drag"
                self.dragging = True
            elif self.phase == "depth_ready":
                panel = cmds.getPanel(underPointer=True)
                if panel and cmds.getPanel(typeOf=panel) == "modelPanel":
                    self.panel = panel
                    self.view = omui.M3dView.getM3dViewFromModelPanel(panel)
                if self.pattern_uuid:
                    target=self.target_name()
                    if not target and getattr(self.footprint,'closed',True):
                        # An untagged curve can pick its Boolean target directly
                        # in the viewport; an explicitly selected mesh wins.
                        hit=core.raycast(*self.ray(*self.press_position),self.collect_meshes())
                        if hit is not None:
                            target=hit.path.fullPathName()
                            self.target=om.MObjectHandle(hit.path.node())
                    # Honor native Undo/CV edits without replacing the curve.
                    self.footprint,_=patterns.read(self.pattern_uuid,mesh=target,normal=self.footprint.frame.normal)
                frame = self.footprint.frame
                _, _, center_x, center_z = self.footprint.dimensions()
                center = frame.point(center_x, 0.0, center_z)
                if self.view is None:self.view=omui.M3dView.active3dView()
                if self.needs_depth_scale:
                    origin,direction=self.ray(*self.press_position)
                    adjacent=core.intersect_plane(*self.ray(self.press_position[0]+1,self.press_position[1]),center,direction)
                    base=core.intersect_plane(origin,direction,center,direction)
                    if adjacent is not None and base is not None:self.units_per_pixel=max((adjacent-base).length(),1e-6)
                    self.needs_depth_scale=False
                self.depth_direction, self.depth_scale, self.depth_fallback = core.normal_drag_axis(
                    self.view, center, frame.normal, self.units_per_pixel)
                self.phase = "depth_drag"
                self.depth = 0.0
                self.dragging = True
        except Exception:
            self.fail()

    def drag(self, *_):
        if not self.dragging:
            return
        try:
            x, y = self.position()
            if self.phase == "base_drag":
                frame = self.footprint.frame
                point = core.intersect_plane(*self.ray(x, y), frame.origin, frame.normal)
                if point is None or (point - frame.origin).length() > self.units_per_pixel * 1e5:
                    self.message("Surface is edge-on. Rotate the view to draw the footprint.")
                    return
                dx, dz = frame.coordinates(point)
                kind=self.snap_mode
                snap=self.native_snap(kind,(x,y),frame=frame,proposed=point) if kind else None
                self.preview.set_snap(snap,self.view)
                if snap is not None:dx,dz=frame.coordinates(snap.point)
                modifiers = cmds.getModifiers()
                self.footprint.set_base(dx, dz, square=bool(modifiers & 1),
                                        centered=bool(modifiers & 4))
                self.preview.update(self.footprint, self.units_per_pixel * 0.1, self.view)
                width, length, _, _ = self.footprint.dimensions()
                hint = ''
                if self.shape == 'box':
                    if modifiers & 1:
                        hint = ' (square from center)' if self.footprint.centered else ' (square from corner)'
                    elif self.footprint.centered:hint = ' (rectangle from center)'
                self.message("Base {:.3f} x {:.3f}{}; release, then drag depth".format(
                    core.ui_distance(width), core.ui_distance(length), hint))
            elif self.phase == "depth_drag":
                # Outward follows the picked face's projected normal, including
                # horizontal/diagonal faces and rolled cameras. Freeze this axis
                # at press so Add/Cut never flips halfway through a drag.
                dx, dy = x - self.press_position[0], y - self.press_position[1]
                depth = (dx * self.depth_direction[0] + dy * self.depth_direction[1]) * self.depth_scale * self.sensitivity
                if self.mode == "add":
                    depth = abs(depth)
                elif self.mode == "cut":
                    depth = -abs(depth)
                self.depth = depth
                shown = depth if abs(depth) > core.EPSILON else self.units_per_pixel * 0.1
                self.preview.update(self.footprint, shown, self.view)
                label = 'Surface' if not getattr(self.footprint,'closed',True) else "Cut" if depth < 0 else "Add"
                hint = " (face-on: up adds / down cuts)" if self.depth_fallback else ""
                self.message("{} {:.3f}{}; release to apply".format(label, abs(core.ui_distance(depth)), hint))
            # Flush the VP2 draw list during the gesture. An idle-only redraw
            # can leave API-created preview geometry invisible until release.
            cmds.refresh(currentView=True, force=True)
        except Exception:
            self.fail()

    def release(self, *_):
        if not self.dragging:
            return
        try:
            self.drag()
            self.dragging = False
            if self.phase == "base_drag":
                if not self.footprint or not self.footprint.valid:
                    self.cancel()
                    return
                self.phase = "depth_ready"
                self.completed_base = True
                self.preview.set_snap(None,self.view)
                self.message("Drag away from the surface to add, into it to cut. Face-on: up adds / down cuts. Q exits.")
            elif self.phase == "depth_drag":
                self.preview.clear()
                if abs(self.depth) < self.units_per_pixel * 2:
                    self.phase = "depth_ready"
                    self.preview.update(self.footprint, self.units_per_pixel * 0.1, self.view)
                    self.message("Drag at least two pixels to set depth. Q cancels.")
                    return
                target=self.target_name()
                closed=getattr(self.footprint,'closed',True)
                if self.depth<0 and closed and not target:
                    raise RuntimeError('Для вычитания выберите кривую и меш или начните жест на нужном меше.')
                # A rename or reparent between gestures must not select another target.
                with core.transaction('Apply Pattern'):
                    uuid=self.save_pattern(select=False)
                    result=core.commit(self.footprint, self.depth, target, self.keep_history)
                    if uuid and self.depth<0 and closed:patterns.retarget(uuid,result)
                self.cancel()
                self.message("Done. Draw the next footprint, or press Q to exit.")
        except Exception:
            self.fail()


class LineSketch(SurfaceTool):
    """Planar click sketch; the native curve becomes the extrusion source."""
    def __init__(self,*args):
        super().__init__(*args)
        self.vertices=[]
        self.angle_step=15.
        self.line_snap=True
        self.reference_directions=[]

    @staticmethod
    def constrain(start,point,directions=(),step=15.,tolerance=4.):
        dx,dz=point[0]-start[0],point[1]-start[1]
        length=math.hypot(dx,dz)
        if length<core.EPSILON:return point,''
        angle=math.atan2(dz,dx)
        candidates=[]
        if step>0:
            increment=math.radians(step)
            candidates.append((round(angle/increment)*increment,'Angle'))
        for a,b in directions:
            if math.hypot(a,b)<core.EPSILON:continue
            base=math.atan2(b,a)
            for quarter in range(4):
                candidates.append((base+quarter*math.pi/2,'Parallel' if quarter%2==0 else 'Perpendicular'))
        if not candidates:return point,''
        difference=lambda value:abs(math.atan2(math.sin(value-angle),math.cos(value-angle)))
        best,label=min(candidates,key=lambda item:difference(item[0]))
        if difference(best)>math.radians(tolerance):return point,''
        # Project onto the guide rather than changing the cursor's distance.
        distance=dx*math.cos(best)+dz*math.sin(best)
        return (start[0]+distance*math.cos(best),start[1]+distance*math.sin(best)),label

    def show_line(self,point=None):
        from .preview import WireOverlay
        points=list(self.vertices)
        if point is not None:points.append(om.MPoint(point))
        if cmds.about(batch=True):return
        if self.preview.overlay is None:self.preview.overlay=WireOverlay()
        overlay=self.preview.overlay
        overlay.points=points;overlay.edges=[(i,i+1) for i in range(len(points)-1)]
        overlay.color=self.preview.color or (82/255.,133/255.,166/255.)
        overlay.bind(self.view)

    def candidate(self,position):
        frame=self.sketch_frame
        point=core.intersect_plane(*self.ray(*position),frame.origin,frame.normal)
        if point is None:return None
        snap=self.native_snap(self.snap_mode,position,frame=frame,proposed=point) if self.snap_mode else None
        if snap is not None:
            point=frame.point(*self._plane_coordinates(snap.point))
            self.preview.set_snap(snap,self.view)
            return point
        self.preview.set_snap(None,self.view)
        if len(self.vertices)>=3 and (point-self.vertices[0]).length()<=self.units_per_pixel*10:
            return om.MPoint(self.vertices[0])
        coordinates=frame.coordinates(point)
        directions=list(self.reference_directions)
        for a,b in zip(self.vertices,self.vertices[1:]):
            ax,az=frame.coordinates(a);bx,bz=frame.coordinates(b)
            directions.append((bx-ax,bz-az))
        value,label=self.constrain(frame.coordinates(self.vertices[-1]),coordinates,
                                   directions if self.line_snap else (),self.angle_step)
        self.message((label+' · ' if label else '')+'Click: point · first point: close · Enter: finish')
        return frame.point(value[0],0.,value[1])

    def _plane_coordinates(self,point):
        x,z=self.sketch_frame.coordinates(point)
        return x,0.,z

    def hover(self,view,panel,position,refresh_meshes=False):
        if self.phase!='sketch':return super().hover(view,panel,position,refresh_meshes)
        self.view,self.panel=view,panel
        point=self.candidate(position)
        if point is not None:self.show_line(point)

    def press(self,*args):
        if self.phase not in ('ready','sketch'):return super().press(*args)
        if cmds.getModifiers() & 8:return
        if cmds.draggerContext(CONTEXT,q=True,button=True)!=1:return
        if self.phase=='ready':
            super().press(*args)
            if self.phase!='base_drag':return
            self.sketch_frame=self.footprint.frame
            self.vertices=[om.MPoint(self.sketch_frame.origin)]
            self.footprint=None;self.phase='sketch';self.dragging=False
            self.reference_directions=[]
            # Explicitly selected reference edges also supply planar guides.
            for edge in cmds.filterExpand(cmds.ls(sl=True,fl=True) or [],sm=32) or []:
                vertices=cmds.ls(cmds.polyListComponentConversion(edge,toVertex=True),fl=True)
                if len(vertices)!=2:continue
                points=[om.MPoint(*(core.internal_distance(v) for v in cmds.pointPosition(v,world=True))) for v in vertices]
                delta=points[1]-points[0]
                if abs(delta*self.sketch_frame.normal)>max(delta.length()*1e-4,core.EPSILON):continue
                a,b=[self.sketch_frame.coordinates(p) for p in points]
                self.reference_directions.append((b[0]-a[0],b[1]-a[1]))
            self.show_line();return
        point=self.candidate(self.position(anchor=True))
        if point is None:return
        if len(self.vertices)>=3 and (point-self.vertices[0]).length()<=self.units_per_pixel*10:
            self.finish_sketch(closed=True);return
        if (point-self.vertices[-1]).length()>self.units_per_pixel*2:
            self.vertices.append(point);self.show_line()

    def drag(self,*args):
        if self.phase=='sketch':
            point=self.candidate(self.position())
            if point is not None:self.show_line(point)
            return
        super().drag(*args)

    def release(self,*args):
        if self.phase!='sketch':super().release(*args)

    def finish_sketch(self,closed=False):
        if self.phase!='sketch' or len(self.vertices)<(3 if closed else 2):return False
        # Native degree-one CVs retain exactly the clicked corners, with one Undo.
        points=list(self.vertices)+([self.vertices[0]] if closed else [])
        with core.transaction('Line Sketch'):
            name=cmds.curve(name='fdSketch#',degree=1,
                            point=[tuple(core.ui_distance(v) for v in list(p)[:3]) for p in points])
            patterns.mark_created(name)
            uuid=cmds.ls(name,uuid=True)[0]
            patterns.retarget(uuid,self.target_name())
            self.footprint,_=patterns.read(uuid,normal=self.sketch_frame.normal)
            from .preview import TURQUOISE
            shape=cmds.listRelatives(name,shapes=True,fullPath=True)[0]
            cmds.setAttr(shape+'.overrideEnabled',True)
            cmds.setAttr(shape+'.overrideRGBColors',True)
            cmds.setAttr(shape+'.overrideColorRGB',*TURQUOISE,type='double3')
        self.pattern_uuid=uuid;patterns.choose(uuid)
        self.completed_base=True;self.phase='depth_ready';self.dragging=False
        self.preview.clear();self.preview.update(self.footprint,self.units_per_pixel*.1,self.view)
        self.message('Sketch saved. Drag to extrude; Q/W/E/R: edit curve.')
        return True

    def cancel(self,*args,**kwargs):
        # Preserve committed clicks when switching to a Maya manipulator.
        if kwargs.get('preserve') and self.phase=='sketch':self.finish_sketch()
        super().cancel(*args,**kwargs)
        self.vertices=[]


class StampTool(SurfaceTool):
    """Repeat a captured cutter; one press/release is one undoable cut."""

    def __init__(self, template, angle=0.0, keep_history=True, selected_only=False):
        super().__init__(template.shape, "cut", template.segments, angle, 1.0,
                         keep_history, selected_only)
        self.template = template
        self.has_dragged = False

    def activate(self):
        super().activate()
        self.message("Copy Cut: click a surface to repeat; hold and drag to position. Q exits.")

    def configure(self, **settings):
        super().configure(**{key: value for key, value in settings.items()
                             if key in ("angle", "keep_history", "selected_only")})
        self.message("Copy Cut ready. Click a surface; Q exits.")

    def update_stamp(self, position):
        hit = core.raycast(*self.ray(*position), self.candidates)
        if hit is None:
            self.preview.clear()
            self.footprint = None
            self.target = None
            self.message("Copy Cut: place the pointer on a visible polygon surface.")
            return False
        self.footprint = self.template.at(hit, self.angle)
        self.depth = -self.template.depth
        self.target = om.MObjectHandle(hit.path.node())
        self.target_path = hit.path.fullPathName()
        self.preview.update(self.footprint, self.depth, self.view)
        cmds.refresh(currentView=True, force=True)
        self.message("Release to copy the cut; hold and drag to position. Q exits.")
        return True

    def press(self, *_):
        try:
            if cmds.getModifiers() & 8:
                return
            self.cancel()
            if cmds.draggerContext(CONTEXT, query=True, button=True) != 1:
                return
            self.press_position = self.position(anchor=True)
            self.has_dragged = False
            panel = cmds.getPanel(underPointer=True)
            self.panel = panel if panel and cmds.getPanel(typeOf=panel) == "modelPanel" else None
            self.view = (omui.M3dView.getM3dViewFromModelPanel(self.panel) if self.panel
                         else omui.M3dView.active3dView())
            self.candidates = self.collect_meshes()
            if self.update_stamp(self.press_position):
                self.phase = "stamp_drag"
                self.dragging = True
        except Exception:
            self.fail()

    def drag(self, *_):
        if self.dragging:
            try:
                self.has_dragged = True
                self.update_stamp(self.position())
            except Exception:
                self.fail()

    def release(self, *_):
        if not self.dragging:
            return
        try:
            if self.has_dragged:
                self.update_stamp(self.position())
            self.dragging = False
            if self.footprint is None:
                self.cancel()
                return
            if not self.target.isValid() or not self.target.isAlive():
                raise RuntimeError("The target was deleted. Click another surface.")
            self.preview.clear()
            result = core.commit(self.footprint, self.depth, self.target_path, self.keep_history)
            if self.selected_only:
                self.selection_roots = [result]
            self.cancel()
            self.message("Cut copied. Click another position to repeat, or press Q to exit.")
        except Exception:
            self.fail()
