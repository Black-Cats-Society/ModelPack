# Model Pack + ReCurve + Fast Draft

**0.1.152-beta** · Windows / Maya 2027

[Скачать установочный ZIP](https://github.com/Black-Cats-Society/ModelPack/releases/tag/v0.1.152-beta)

Распакуйте весь ZIP в постоянную папку с латинским путём. Выберите полку в Maya,
перетащите `install_model_pack.py` в окно 3D-вида и сохраните полку через
**Shelf → Save All Shelves**. [Инструкция по установке](INSTALL_RU.txt).

### Nonplanar HP

Откройте **Model Pack → Mesh → Nonplanar HP**. Кнопка **Set LP** назначает редактируемый меш.
**Set HP** принимает один или несколько опорных мешей. **Threshold** выделяет
грани с отклонением выше порога. **Triangulate by HP** разбивает найденные грани
диагоналями по опорным поверхностям. Триангуляция отменяется одним **Ctrl+Z**.

### ExtrudeX

Для кнопки **ExtrudeX** скачайте оригинальный комплект Joe Wu 1.01 у автора.
Скопируйте `extrudeX.py` и папку `plug-ins` из этого комплекта в
`vendor/extrudeX/` внутри распакованного Model Pack. Структура папки:
`vendor/extrudeX/extrudeX.py` и `vendor/extrudeX/plug-ins/2027/extrudeX.mll`.
Модуль должен соответствовать версии Maya.


## Floater Fit (Surface Fit 0.1.10)

Integrated into the supplied Model Pack 0.1.82. All other tools, including
Fast Draft, Edge Flow, Auto Merge, Offset, Quad Dome and Floater, use that build.

Select one polygon mesh and click **Floater Fit** (next to **Floater**). Drag its plane onto another
mesh; curved handles rotate, cyan arrows adjust **Gap**, and the centre square
changes uniform **Scale**. X/Y/Z chooses the mounting axis; click again to flip
its sign. The lowest signed side docks to the surface and the pivot retains
the placement orientation (Maya Object mode).

**Ctrl+D** creates an independent editable floater and activates it. Select a
previous floater in the Outliner to return to it during the session. After
finishing or reopening the scene, select any result and click Floater Fit again.
Each result keeps its own editable history. Undo works per step while active;
after finishing, one Undo reverts the whole session.

**От открытого края** is enabled initially for open meshes. Its initial curve is
(0,1), (0.25,1), (0.75,0), (1,0). Every curve point can move horizontally and
vertically. Existing edited curves and Ctrl+D copies keep their settings.

Scale and Gap preview before the first snap. The original mesh is temporarily
hidden and restored on Finish, Cancel or window close. Selection wire is hidden
during work; Maya Wireframe on Shaded is preserved. New cuts are incorporated
on the next adjustment or with **Обновить геометрию**. Large N-gon caps receive
interior samples on the result. Mesh Smooth of the target preserves attachment.

Install the entire folder with the supplied installer. To reload the installed
copy without restarting Maya, run this in Maya's Python tab:

```python
import runpy
runpy.run_path(r"C:\MayaTools\ModelPack\reload_model_pack.py", run_name="__main__")
```


**Integration build 0.1.80-recurve-fastdraft**, based on the supplied Model Pack

**Integration build 0.1.101-recurve-fastdraft**, based on the supplied Model Pack
+ ReCurve 0.1.3 and Fast Draft 0.1.3. ReSegments and Control Curve remain in the
existing Model Pack grid. Their settings expand directly underneath in the same
style, with one shared live preview.

**Make Planar** projects the vertices of selected polygon faces onto one plane.
Auto uses their best-fit plane. To use an explicit reference, select faces or
vertices and click Set Reference in the tool panel. Select the faces to flatten
and Apply. Pin fits a plane through the reference vertices. Perpendicular fits
a plane at 90 degrees to the reference faces or to a plane defined by three or
more coplanar reference vertices, keeping shared vertices fixed. Each Apply
is one Undo step.

**Floater Pattern** fills a mesh or selected faces with Grid or Staggered rows.
Pick Source, Surface / Faces, optional closed Boundary and closed Exclusions.
Max per Row, Max Rows, Rotation, Edge Margin and Surface Offset control the layout.
Live Preview uses instances and updates while mask CVs or transforms change.
Create commits instances or copies in one undoable group and freezes the layout.
Footprint checks include the source skirt. Surface placement uses a projected
surface patch with local normals, suitable for panels and gently curved regions.
Guides adds closed contour rows. Guide Flow defaults to Guide Waves: parallel
offset rows continue outward from each guide to Boundary or the surface border.
Inside the innermost guide, the original center-to-guide construction creates
complete nested rows, sampled uniformly by arc length, with alternating offsets.
These scaled contours retain the guide shape without inward offset collapses.
Staggered offsets alternate wave rows. Guide CV and transform edits rebuild
the waves in Live Preview. Choose Mosaic for a lattice that blends
between guide contours and the outer border.
Max per Row sets the longest row count; shorter rows scale their counts.
Max Rows sets the row budget. Source size limits density to avoid overlap.
Guided layouts anchor complete rows on the guide contours and an inset of the
outer border. In Mosaic mode, a deterministic centroidal lattice fills the
interior and transition bands, with spacing constraints and fixed contour anchors.
Staggered starts from a hexagonal lattice and is the default. Grid starts from
a rectangular lattice. Cells adapt together where the guide meets the border.
Staggered offsets alternate rows. Guide CV and transform edits update Live Preview.
Multiple guides must be nested, disjoint contours visible from a shared interior
point. Neighboring floaters are conservatively separated using the source bounds.
Live updates reuse preview instances and cached surface data. Boundary tests use
nearby segments, vectorized masks and spatial neighbor buckets.

**Bevel** checks expansion contacts within each connected mesh shell. Separate
parts in a combined mesh can overlap without limiting each other's bevel width.
Residual-face collapse and self-contact within a connected shell still limit
the width. The rule applies to preview, saved history and committed geometry.

**Curve Editor** provides Bevel for selected curve CVs. Choose the CVs, open
Curve Editor and press Bevel. Drag LMB or change Width for the fillet size.
Shift + Wheel adjusts its Points count from 3 to 64 (higher-degree curves have
a higher minimum). Apply/Enter commits, Cancel/Esc restores the original.
Open and closed curves, including periodic Pencil Curve strokes, are supported.
The unedited spans keep their shape. Curves with incoming construction history
must have that history deleted before editing.

**Slide Bevel** lets opposing loops meet on narrow quad strips while wider
parts continue moving. Their meeting vertices are welded into one contour;
zero-width strip faces are removed by Maya's native merge. Contacts are local
to each rail rather than limiting the whole face. This also works for several
additional loops and both Uniform/Proportional modes. Pulling back reopens the
loops from the original geometry. Preview, the editable result, Apply and
Undo/Redo use the same contact geometry. Clone Slide retains its existing limits.
Concave corners use inward guides instead of rails that point outside the
face. Interior miter points and fully selected faces join at their local
collapse, and nested corner bands join the older contour when they collide.
Several loops build their topology before moving to the requested width, so
already collapsed thin areas do not interfere with constructing the next loop.
The Width slider updates the live preview and commits on release. Enter an
exact width in scene units for Uniform or in percent for Proportional.
The slider covers up to one scene unit in Uniform mode. A regular turn has a
spoke joining the corner to its inset. At a folded triangular transition,
contours cross the selected junction edges and transverse cuts form quads
on the adjoining strips. These cuts continue through nested contour bands.
Uniform width is measured from both selected edges where an almost planar
strip turns across a shared seam.

**Floater** builds a separate recessed ribbon along selected curves.
You can also select mesh edges and click **Floater** directly. Each unbranched
chain or closed loop is first converted with Maya's native Poly To Curve to a
degree-one editable curve, without construction history or smooth-preview
conformance. Separate chains and meshes produce separate curves; each remembers
its own support mesh/instance. The original mesh is unchanged. Edge selections
on an existing floater create new curves rather than reopening its settings.
The conversion is one native Undo step before the live preview. Cancel leaves
the converted curves available for editing/reuse; Undo removes the conversion.
Apply creates the floater in the following Undo step. Curve CV edits remain live.

Select a curve and one support mesh to fit the groove to a curved surface.
For a curve already drawn close to a mesh, Floater finds the nearest visible
support automatically; ambiguous candidates require an explicit selection.
Fast Draft patterns retain their tagged support. The source curve and support
mesh remain unchanged. The skirts follow the support's curvature at Height,
and their authored normals match its interpolated face-vertex normals for a
smooth shading transition. Height follows local surface normals. Rows retain
the source curve's corners and add sections at actual bends between support
polygons. Changes of interpolated normals inside a flat face add no loops.
The groove uses five profile points. Skirts add one support loop next to each
lip, producing nine profile points before surface bends. Each support stays
Opening Width / 8 from its lip when Skirt grows; very short skirts clamp this
distance to half the skirt. Both sides use the same distance. Supports follow
the same surface and Height as the skirt.

Across a curved support, the skirt follows unfolded mesh faces at world-space
arc distances. Smooth shading normals control Height and shading rather than
the surface offset. Real bends add matching quad columns; flat triangulation
adds none. Sub-tolerance bends are simplified without moving groove/lip/support
anchors. Every quad is checked for folds. Closely spaced crossings that would
fold a full-width strip use geometry-error sampling along the path instead;
an invalid offset keeps the last valid preview.

While Floater is active, drag in the viewport with **Shift + LMB** for Opening
Width, **Shift + Ctrl + LMB** for Height, or **Shift + Alt + LMB** for Skirt. Right/up increases
the value; left/down decreases it. Release LMB or the modifier to finish the
gesture. Esc during a drag restores its starting value; Esc again cancels the
tool. Ordinary viewport gestures remain available outside these combinations.
RMB and its context menus remain available while Floater is active.
Ctrl + LMB and Alt + LMB without Shift retain their native Maya behavior.

Surface bindings remain live after Apply and save in MA/MB: moving/deforming
the support or editing curve CVs updates the ribbon. Selecting an existing
floater and a mesh adds or changes its support; Cancel restores the old binding,
while Apply supports one Undo/Redo. Delete History bakes the floater without
deleting the curve or support. Free curves retain the behavior below.

The curve marks the groove's center/bottom. **Opening Width** is the distance
between the two upper lips, **Height** raises those lips above the curve plane,
and **Skirt** extends a flat band from each lip. A rounded cross-section and
authored normals keep the skirts flat and blend the groove smoothly. The mesh
uses quads and a continuous planar UV island. The underlying object is not cut.

Select one or more ordinary Maya/Pencil curves (or their CVs), click **Floater**,
adjust the three fields below the grid, then **Apply / Enter**. Values use the
current scene units with three decimals. **Esc / Cancel** removes the preview;
closing Model Pack cancels it. Switching to another Model Pack function applies
a valid preview. W/E/R remain available for native curve editing while the panel
is open; changing applications retains it. Auto Merge pauses during the preview.

After Apply, the source curves remain connected: edit CVs, rotate/scale/move the
curve, or change its native construction history to update the floater. The
three sizes are also keyable in the `modelPackFloater` Inputs node. Selecting an
existing floater or its curve and clicking Floater reopens its settings; Cancel
restores the previous sizes. Batch editing and Apply support one native Undo/Redo
step. History saves in MA/MB, and Delete History bakes the mesh while retaining
the curve. Freehand curves with depth variations retain their actual 3D centerline;
the ribbon follows that depth and its authored normals follow the local slope.
Opening Width and Skirt are measured in the curve's fitted main plane. That
projection must remain a simple path; overhanging or intersecting projections
need a separate curve. Open curves make open-ended ribbons; closed curves make
closed grooves. Self-intersections and oversized offsets are rejected;
a temporarily invalid edit retains the last valid preview and disables Apply.
Only offset failures trigger narrower starting defaults. Invalid source paths
report their actual error without shrinking into a false sampling-limit warning.

**Reset Tube** keeps each closed end as one n-gon when changing the section
count, with no internal cap edges or center vertex. This also replaces an
existing triangulated/fan cap. Open ends and closed-loop tubes remain open or
cap-free respectively. Body edges stay soft, cap rims stay hard, and the UV
layout retains its existing seams. The live history and direct script entry
point use the same cap topology.

**Bevel junctions** now use Maya's native Auto mitering and Subdivide N-gons
behavior. One segment has the standard corner triangles without extra cuts in
neighboring bevel strips. Additional segments use the same native corner layout
as Maya, including its triangles for odd counts. Editing Shape never changes
Segments automatically, including edits to baked bevels. The 60-degree boundary
normal rule, width controls and zero-width finalization remain available.
Independent bevels ending in an existing coplanar face fan retain a single flat
end n-gon, avoiding the old floor triangulation. Preview, Apply and Channel Box
segment changes use the same topology. Saved
histories made with the former forced-quad corner fill retain their layout until
explicit re-edit; opening them in Bevel and applying uses the native layout.

**Quad Dome** replaces a selected flat convex face or connected coplanar patch
with a rounded all-quad cap. Its standard-width button sits next to Offset.
**Height** uses scene units (three decimals): positive values create a dome,
negative values create a recess. Dragging can cross zero in either direction.
At zero the original surface is restored, without new geometry or an Undo step.
Recess normals face the cavity and its lip remains hard, keeping the outer
side's normals independent. **Rings** controls the latitude
bands (1–32). Drag LMB for height or Shift+Wheel for rings. Apply/Enter accepts,
Esc cancels; Q/W/E/R or another tool accepts the last valid preview. Closing
Model Pack cancels, and changing applications preserves the preview.

Odd boundaries gain one extra section, because an all-quad disk requires an
even perimeter. **Split Side Strip** is on by default and continues the section
through adjacent quads until an existing non-quad or open boundary. On circular
or elliptical sections the added sample follows the fitted arc, preserving a
smooth dome rather than introducing a chord-shaped dent. Existing vertices
stay fixed. Turning the option off only splits the rim and may leave a side
n-gon. A latitude-to-grid crown avoids a triangulated pole. Analytic ellipsoid
normals match across the cap and shared rim; unrelated shading is retained.

Supports one convex planar contour with 3–128 sides on a local, non-instanced
mesh. Holes, concave/bent regions and ambiguous side strips fail safely. The
selected region needs one material; other face materials remain assigned.
Existing UV sets and colors remain; new cap UVs use planar mapping and new cap
colors inherit the boundary's average paint. Apply supports native Undo/Redo,
Delete History and MA/MB saving.

**Offset** creates an inset in a selected polygon or connected face region. On a
planar or slightly warped contour it keeps the original
rounded-corner radii and samples. Tangent-supported rounded corners translate
rigidly while the straight sides move inward; the radii do not shrink with the
offset amount. The tool has one standard-width button and a bottom panel with
an **Offset** field (three decimals), **Apply** and **Cancel**. Drag LMB horizontally
in a viewport or enter a value. Enter applies; Esc cancels. Q/W/E/R or switching
to another Model Pack function applies the preview and exits. Closing the window
cancels the pending preview. Switching applications leaves the current settings
and preview intact. Oversized offsets stop before a contour collapses or crosses
itself. Applied geometry uses native Maya extrusion/tweak history and one
Undo/Redo step. Slightly warped polygons retain each original vertex's height
above the support plane, so the rounded corners still translate rigidly in 3D.
On a curved region with a non-overlapping projection, Offset keeps the inner
face layout and places the inset vertices on the original surface. The width
stops before the internal faces collapse. Fully circular contours, regions
with holes and regions that overlap in projection require a different selection.

**Pencil Curve** draws editable freehand curves with a small number of CVs.
Hold LMB in a viewport, sketch, then release to fit the curve. Straight strokes
within tolerance use exactly two CVs. Other strokes use a cubic fit followed by
span reduction; candidates are checked against the drawn shape before replacing
the fit. **Simplify (px)** defaults to **2.00**: increase it for fewer points and
smoother curves, decrease it for a closer match. Results are ordinary scene
curves with no drawing history or custom plugin requirement.

**View Plane** draws through the clicked mesh point, selected object's center or
camera focus. **Surface** follows the initially picked visible mesh and ends at
the first missed surface hit. **World XY / XZ / YZ** provide fixed drawing
directions through the picked/selected point (world origin without a target).
**Auto Close** makes a periodic curve when the stroke returns within six pixels
of its start. **Edit Points** leaves drawing and selects the new curve's CVs
with Move active. Esc cancels only the pending stroke; Q/W/E/R leave the tool.
Finished curves remain after leaving or closing Model Pack, save with the scene,
and each stroke supports one native Undo/Redo step.

Fast Draft occupies one standard-width button below that grid. Its embedded panel
contains a Cylinder / Cube choice, a center-snapping checkbox and a read-only
section-count hint, plus a **Scene Curves** list.
No separate Fast Draft installation is needed.

**Ordinary scene curves:** select a Maya NURBS curve or a Pencil Curve result
and click **Fast Draft** to drag its extrusion depth. No Model Pack pattern tag
is required. Open curves create open polygon walls for either depth direction;
their endpoints remain separate. Two-point lines, smooth/rational curves and
spatial open strokes retain their sampled 3D shape. Closed planar curves create
capped solids, with concave boundaries supported. For subtraction, select the
curve together with one target mesh and drag inward, or select only the curve
and begin the depth gesture on the target mesh in the viewport. Explicit mesh
selection takes priority over objects under the pointer. Open walls do not use
the solid Boolean path. Close the contour in Maya or use Pencil Curve's Auto
Close when a solid cutter is needed. Source curves retain their native editing
and history, and successful cuts remember the visible result as their next
target. Multiple curve shapes under a transform can be selected individually
in the Outliner; each is listed separately in Scene Curves.

**Persistent 2D patterns:** release the base gesture, then leave Fast Draft with
Q/W/E/R, another tool or window close. The turquoise outline remains as a normal
scene curve named `fdPattern#`, with its pivot at the pattern center. Select it
and edit its CVs or use Maya's W/E/R, then reopen Fast Draft
to extrude the current curve boundary. Selecting CVs also identifies the pattern.
The **Scene Curves** list immediately resumes the chosen contour.
Esc still cancels the pending drawing/edit;
an incomplete base drag is discarded. Add/Cut also retains the outline. Curves
survive scene save/reopen without a custom pattern plugin.

Edited patterns may be convex or concave. Rebuilt smooth curves use evaluated
curve samples rather than their control polygon. The contour must remain planar
and not intersect itself. Caps stay n-gons without center vertices; the curve's
CVs, knots, degree, history and transforms remain intact after Add/Cut or exit.
Unchanged boxes/cylinders retain their existing native primitive parameters.
**Edit Pattern**, **Rotate** and **Align to Edge** have been removed; ordinary
Maya curve editing supplies these changes. Target message connections follow mesh rename/reparent;
after subtraction the pattern targets the visible Boolean result for reuse.

**Edge Flow** opens a compact bottom panel with **Relax**, **Space** and
**Set Reference / Parallel**. Relax and Space
support selected edge chains and strips of selected transverse edges. For a
strip, its two longitudinal boundary contours supply the directions. Relax
averages neighbors along each contour, preserving its plane instead of pulling
the wall's top and bottom toward one another. Space uses equal arc-length steps
along those contours. Open ends stay fixed; closed loops keep one anchor vertex.
Separate strips/chains and meshes are independent. Each click is one Undo step
and keeps the edge selection, topology, UVs and smoothing flags.

For **Parallel**, select one reference edge or several parallel reference edges
and press **Set Reference**. Then select the edges to change and press
**Parallel**. Independent edges rotate about their centers; connected open
chains straighten along the reference direction, preserving each edge length
and the chain's vertex centroid. Reference edges and shared reference vertices
stay fixed. Several meshes can be processed in one Undo step. A reference follows
rename, reparent and world transforms; changed reference topology requires a new
Set Reference. Closed loops, branches and incompatible fixed endpoints are
rejected before any geometry changes. Closing the panel clears its reference.
With Maya's Track Selection Order already enabled, Parallel also accepts the
last selected edge as reference without Set Reference. Model Pack does not
change this global preference.

**Select → Path** is a checkable mode in the bottom panel and Select's right-click
menu. Enable it, select a starting vertex/edge/face, then **hold Shift and click once** on
the destination on the same mesh. An unselected destination adds the shortest
path; an already selected destination removes it. **Ctrl+Shift-click**
always adds. Keep Shift held to continue clicking successive endpoints.
Unmodified clicks, Ctrl-click, marquee, Alt navigation and plain double-click
keep Maya's behavior. Paths work in Select and native Move/Rotate/
Scale contexts; they preserve other selected components and masks/constraints.
After adding a path, its clicked endpoint becomes the anchor for the next path.
Toggle Path off to restore native Shift-click immediately. Closing Select,
starting another Model Pack function or changing scenes disables the mode.

Distances use world-space lengths, including nonuniform scale. Vertex paths
follow edges; edge paths join at vertices; face paths cross shared edges. No
geometry/history is edited. Select-context gestures combine the native endpoint
pick with Path into one Undo step; manipulator contexts retain their native
first-click Undo. Disconnected shells retain the native endpoint pick. The
existing `model_pack_selection.ui.path()` API still selects a path from exactly
two explicitly selected endpoints as one native Undo/Redo step.

**Select → Вдоль кольца / Поперёк кольца** repeats the entire connected selection
group with a gap, for polygons and edges. The **Пропускать элементов** field is
the number of faces/edges to leave between copies in the travel direction.
For example, a three-face-wide group with gap 2 repeats every five positions;
its complete width and the offsets between neighboring rows are preserved.
Polygons follow the longer quad loop or its cross direction. Edges follow their
edge loop (**Вдоль**) or edge ring (**Поперёк**). Parallel edge groups and whole
edge loops can be repeated as complete groups too.

Open strips stop before clipping a copy at the boundary; poles and inconsistent
quad joins stop or reject the repetition. Closed loops are traversed once,
keeping complete copies; an incomplete step remains at the seam. Separate
connected groups/meshes are independent. Selection stays in face/edge mode and
each action supports one native Undo/Redo; geometry, UVs and normals are unchanged.

**Auto Merge** toggles automatic welding for the entire active mesh when it is
selected and after geometry/transform edits. With several meshes selected, only
the last active mesh is checked. It stays enabled across Model Pack functions
while the window is open; a new window starts with Auto Merge off. Right-click
the button for **Distance (cm)**, default **0.001 cm**, independent of scene units.
It waits for mouse release and ordinary selection/move/rotate/scale contexts;
live Model Pack previews, text input, playback and other applications pause it.
Each weld is a separate **Auto Merge** Undo step. Undo leaves the restored mesh
available without immediately welding it again; a later edit/selection resumes
checking. UV seams and upstream history are retained. Native merge history can
reevaluate its threshold when upstream geometry changes. Closing/hiding Model
Pack stops merging; instances, referenced meshes and locked inputs are skipped.
Merge arguments no longer become the current vertex selection. Objects remain
selected as objects; selected vertices, edges and faces are remapped to the
welded result, with selection masks, hilite and the previous manipulator retained.
Redo restores the same selection intent. Identical mesh/threshold states are
not retried repeatedly when native Maya makes no change; changing geometry,
Distance, re-enabling the toggle or selecting after Undo permits a new check.

**Plane Deform** moves a selected face or vertex patch together, including
corners meeting triangular caps. Triangles may rotate instead of locking the
whole patch. Other supporting planes guide the movement where possible; a
fully constrained corner follows its source edge. The common drag stops before
the fixed outer row or collapse of a selected edge. Helper tilts, Cancel and
Undo/Redo use the same geometry in cm, mm and m scenes.

New Bevels keep their end caps as N-gons without inserting an extra fan of
vertices and edges. Multi-segment strips retain soft internal edges. Every
boundary of the created strip uses a **60-degree world-space threshold**:
angles up to 60 degrees are soft; larger angles are hard. This also applies to
single-segment chamfers and end-cap borders. Width, profile and segment changes
in the live tool or Channel Box recalculate the boundary flags. Unrelated edge
flags remain native; imported locked normals are released only on affected
corners. Previously saved subdivided caps retain their topology on reload.

Bevel accepts edge selections named on either a transform or its mesh shape,
including API selections and Select Path results. Enter and leaving the tool
retain the previewed geometry in both cases; saved bevels remain editable.

Editing an existing Bevel down to Width 0 keeps its strip reversible while the
tool is active. Apply or leaving the tool then welds each section, removes the
collapsed polygons and makes the resulting sharp edge hard. Esc cancels the
edit. A small positive width keeps its geometry.

Created cylinders have soft side edges and hard cap borders. Their native
polyCylinder history and Model Pack cap node expose **Radius**, **Height**,
**Sides** and **Top Cap** directly on the transform in the Channel Box. Top Cap
offers **Cap** (one N-gon), **Center** (a triangle fan with one central point), and
**Parallel** (parallel strips connecting existing rim vertices). The bottom cap
remains an N-gon. Cap changes preserve UVs and support Undo/Redo and scene reload.

With Model Pack open, select a created cylinder and press **T** in the viewport
to choose Cap / Center / Parallel. T exits idle Fast Draft drawing and opens the
cap menu; for other objects, text inputs and other active Model Pack tools it
keeps its ordinary Maya behavior. Channel Box parameters also work after closing
the toolbox. Created cylinders retain their editable history even when drawing
history is globally disabled.

Cylindrical cutters always use N-gon caps, without central vertices. Boolean
results soften only the cylinder-wall seams; cap boundaries and unrelated target
edges keep their existing hardness. Blind cylindrical cuts have an N-gon floor,
including when Boolean history is baked.

Drawing axes follow the object's local axes projected onto the picked surface,
with a perpendicular extrusion normal. An irregular polygon's longest diagonal
cannot rotate the cylinder pattern. Preview and committed rim positions agree.
Subtraction accounts for float precision away from the world origin, then
dissolves new near-coplanar Boolean diagonals, including cap triangulation and
outer radial bridges. Original mesh edges remain protected. A planar outside
face can have an inner hole loop; no artificial radial spokes are required.

## Fast Draft

Click **Fast Draft**, choose **Cylinder** or **Cube**, then drag on a visible mesh
to draw the footprint. Release, then drag outward along the surface normal to add
a separate primitive, or inward to subtract from the picked mesh. Release to commit. Each completed
operation supports native Undo/Redo; the tool remains ready for the next drawing.

**Привязка к центрам** is enabled by default and remembered. Before the first
click, move within 12 screen pixels of an edge midpoint or the center of the
polygon under the pointer. A turquoise diamond/cross and label
show the exact anchor. Start drawing there to snap the cylinder center or the
cube's first point; **Ctrl** centers the rectangle on that anchor. Snapping
sets only the initial point, so the base size and depth gestures stay free.
Turn the checkbox off for free placement. Centers use the average position of
the polygon's vertices in world space. On a warped polygon, that average is
projected along its normal onto its own triangulated surface, so it remains a
usable surface anchor. Qt hover coordinates are converted to native viewport
pixels on scaled/HiDPI displays, matching the first click and marker.
A center outside a concave face, in a hole, on a different
polygon or hidden behind geometry is rejected. Hovering does not create scene
nodes or change selection.

Momentary snapping also applies to the first point and the moving base corner
or cylinder radius: hold **V** for mesh vertices, **C** for NURBS curves, or **X**
for grid intersections. Release the key to return immediately to free drawing,
even with a still mouse. For one vertex anchor, hold V, start the base on that
vertex, release V, then drag the free corner/radius. The chosen origin stays fixed.
These modes
take precedence over automatic polygon/edge centers and work with the centers
checkbox off. Fast Draft tracks its own held keys rather than persistent Maya
toolbar flags, which can remain enabled after Modeling Toolkit release commands.
It reads Maya's snap tolerance without changing global snap flags or key bindings.
Text fields retain their shortcuts. Focus leaving the application, cancellation,
shape changes and tool shutdown clear held keys. Auto-repeat cannot latch a mode.

The base stays in its picked surface plane: point targets must lie in that
plane; curves can lie in it or intersect it. Grid snapping uses scene spacing,
subdivisions and units, rounding the two main world axes of the drawing plane
and solving the third coordinate on the plane. Starting C/X anchors must remain
on the picked mesh; V can also pick a nearby silhouette vertex. Snapping never
changes the depth gesture. Shift/Ctrl base constraints apply after snapping.

**Shift+Wheel** changes the active footprint without discarding its position or
depth: 3–256 sides for a cylinder. Each wheel notch changes the count by one, and
the cylinder count is remembered. A cube always has six faces and no subdivisions;
old cube subdivision preferences are ignored.
With no unfinished drawing, the wheel sets the count for the next primitive.

When drawing a Cube base, hold **Shift** for a square with the first click as a
corner, or **Ctrl** for a rectangle centered on that click with independent width
and length. **Shift+Ctrl** combines both: an equal-sided square from the center.
Moving the cursor sets the corner; centered drawing extends equally in opposite directions.
Modifiers can change during the base drag, including with a still mouse. After
releasing the mouse the base stays fixed while setting depth, even if the keys
are released. Without Shift, width and length remain independent.

**Esc** immediately discards the unfinished drawing and leaves Fast Draft ready
for a new base. **Q / W / E / R** discard the drawing and switch to Maya's Select /
Move / Rotate / Scale tools. This works during the base drag, while waiting for
depth, or during the depth drag, including before releasing the mouse. Text
inputs and modified shortcuts keep their normal behavior. Native tool changes
also close the Fast Draft settings and remove the preview. Clicking Fast Draft
again or choosing another Model Pack tool exits. Changing shape cancels only the unfinished
drawing. Closing/reloading Model Pack or opening a new scene also clears its
preview and pending wheel input. Completed geometry stays in the scene.

The outline matches Maya's blue/teal active toolbar buttons (`#5285A6`) throughout
both gestures. An input-transparent composited viewport overlay paints display
sRGB pixels, independently of VP2 template/reference wire colors and scene color
management. Its temporary API mesh is hidden; selection and Undo stay untouched.
Depth follows the projected outward normal at the footprint center, including
horizontal/diagonal faces and rolled cameras. Its screen direction is fixed for
each depth gesture. With a face-on view, where the normal has almost no screen
projection, up consistently adds and down cuts; the viewport message explains this.
The signed-depth status message indicates Add / Cut. Ordinary wheel and modified
camera navigation keep their usual behavior. Ctrl+Shift+Wheel remains available
to Control Curve when that tool is active.

This update limits Shift+Wheel to active ReSegments, adds live segment Undo,
shows the active tool's shortcut hint, and fixes the panel width while switching tools. ReSegments
uses the evaluated mesh, so you do not need to delete or enable object history.

## Install

1. Extract the entire archive to a permanent folder, such as
   `C:/MayaTools/ModelPack_ReCurve`. Keep all scripts and the `icons` folder together.
2. In Maya, select the Shelf tab where you want the Model Pack button.
3. Drag `install_model_pack.py` from the extracted folder into a Maya viewport.
4. Save the shelf with **Shelf > Save All Shelves**.

The installer updates the existing MP shelf button and `ModelPack.mod` entry.
Opening Model Pack loads the included ReCurve runtime and retains **Shift+R**.
An older or separately installed ReCurve copy is updated with its existing safe
updater; an unfinished ReCurve preview is cancelled before replacing that copy.
Reinstall after moving the package. No separate ReCurve installation is needed.

If drag and drop is unavailable, run this in the Script Editor's **Python** tab,
using the path where you extracted the package:

```python
import runpy
runpy.run_path(r'C:/MayaTools/ModelPack_ReCurve/install_model_pack.py', run_name='__main__')
```

## Shared Mirror Cut / Mirror Dup plane

Mirror Dup includes the same green point buttons halfway along the viewport
gizmo's X/Y/Z arrows as Mirror Cut. Click a point to switch the reflection axis
for all selected meshes. Clicking the active axis again leaves it selected;
Mirror Cut retains its existing repeated-click side reversal. Plane position,
rotation and World/Local settings are preserved, with native Undo/Redo.

Selecting multiple meshes creates one reflection plane and one viewport
manipulator for the entire selection. Its initial origin is the combined
selection bounding-box center. **World** uses world axes; **Local** uses the last
selected object's world rotation, excluding scale and shear. Each mesh retains
its own transform and result, with all reflections evaluated against that same
plane. A single selected mesh keeps its existing World / Local pivot behavior.

Moving, rotating, entering an angle, snapping the plane, changing the mirror axis
or reversing the cut direction affects every selected mesh together. Undo/Redo
and cancelling a plane drag restore all members together. The plane outline
spans the whole selection. Closing the tool applies all results as before;
Mirror Cut also handles a member lying entirely on the discarded side.

## ReSegments

1. Select one or more separate edge chains or rings on a polygon mesh.
2. Click **ReSegments** at the bottom left. Live preview starts automatically.
3. Change the segment count with minus/plus, type a value, or use **Shift+Wheel**
   over the viewport while ReSegments is active. Wheel input does not start the
   tool in the background. The displayed value applies to each selected chain.
4. Choose **N-gons** or **Triangles**, or tap **Alt** to switch.
5. Click **Apply**, or press **Space / Enter** over the viewport. **Cancel / Esc**
   restores the mesh from before the preview.

Only the selected chains change their count. Matching adjacent chains connect
in both modes. N-gons keeps surrounding faces without adding diagonals;
Triangles splits affected surrounding n-gons where needed. Unselected chains
retain their own vertex count.

## Bevel history cleanup

After applying Bevel, select the object and use Maya's **Edit > Delete by Type >
History**. This bakes the visible result and removes the Bevel node, native
templates and segment parameter source. Geometry, UVs, normals and materials
remain unchanged. Normal Maya Undo/Redo works. Non-Deformer History is also
supported for meshes without deformers.

The Channel Box **Segments** control and the Bevel panel/gestures still change
both templates together. Their shared parameter is upstream of the templates,
so deleting history does not leave a disconnected cycle in the scene. Existing
connected histories from earlier Model Pack versions are upgraded when opening
or reloading the toolbox; opening old .ma/.mb files remains supported.

## Editing an existing Bevel without history

Select polygons of a recognizable bevel band, then click **Bevel**.
Narrow collars next to a broader curved fillet can also be picked from one face.
The local search follows the width break across their profile, retains nearly
tangent end facets and stops at reconstructible terminal cells. This fixes the
two-row front rim in the newer Detail04 FBX without including its broader fillet
or hole transition. **Find Bevels from selection** uses the same recognition for
UnBevel. An explicit complete polygon band still defines its own exact extent.

The band is recovered from its geometry and the current shape opens unchanged,
with its estimated width, segment count and profile. A complete selected polygon
band takes priority over automatic search, including where it crosses narrower
longitudinal fillets. Select all rows to edit the whole cross section, or just the
rows to adjust. A single picked face expands to its recognized strip.

Terminal triangle fans and polygon caps with a fixed root may remain outside
the selected band. Faces that reduce to an edge at zero width are treated as
terminal fill; other support faces retain the orientation checks. This includes
imported strips with several segments and tessellated end caps. Per-face material
assignments survive Apply, Undo/Redo and scene save/reopen after width/profile edits.

Explicit single-segment strips can follow bends with warped support faces.
If the outgoing support directions are skew, the surrounding strip determines
the transverse section plane used to reconstruct the corner. This supports the
full longitudinal chamfer in the latest Detail04 example for both Bevel editing
and UnBevel removal, while retaining the checks against flipped support faces.

Every edge selection creates a new bevel on exactly those edges, including edges
on or next to an existing bevel. Edges never activate neighboring bevel editing.
Whole-object selections do not open existing-bevel editing either. The edit menu
also requires bevel polygons. When saved Model Pack history is available, its
parameters are reopened only for polygons belonging to that bevel's current
output; otherwise the selected polygon band is recovered from geometry.

Use the same numeric controls and gestures: drag for width, **Shift+drag** for
segments, **Ctrl+drag** for profile and **Shift+Ctrl+drag** for local width.
The selected band is resolved before Maya changes to the tool's edge-selection
mask. Its preview is highlighted for viewport editing. The visible transverse
end profiles also accept local-width grips within 14 pixels, including just
outside the silhouette.
Numeric arrow/wheel steps update the preview immediately. Typed values commit
with **Enter** or focus loss; both decimal point and comma are accepted. Enter
in a number field confirms that value and keeps the Bevel session open.
Width and shape fields display three decimal places; segment/cut counts stay
integers. Synchronizing the display does not round the live geometry parameters.
**Enter** applies one undoable mesh edit; **Esc** restores the original mesh.
Session Undo/Redo, custom profiles, saved UV sets and scene save/reopen work.
Changing segments rebuilds the selected cross sections; it does not stack a
second bevel on top. Shape and placement outside those sections stay unchanged.

This mode edits strips and closed bevel bands between corner junctions, including
single-segment chamfers. Complex junctions must be excluded from the selection.
Manual polygon editing also accepts connected bevel networks with junctions.
Their width follows local bevel normals in the tangent space of the supporting
surface. Existing transverse support rows translate with the core rim, retaining
their original gaps. Narrow bevels retain their local width proportions; the
preview stops before an original triangle folds. Curved longitudinal loops are
free to change direction as the width changes.
Width and shape can retain multiple materials; changing segments requires one
material. Vertex-color meshes, references, instances and locked inputs are not
supported. Transverse-cut and equal-width mode switches remain available when
creating a new bevel; a recovered band retains its existing longitudinal cuts
and uses a shared physical width limit.

## Control Curve

1. Select edge chains or a region of polygon faces on one mesh, or keep an
   existing ReSegments preview open.
2. Click **Control Curve** at the bottom right. One fitted NURBS control curve
   is created for each selected chain or face-region contour, and Maya enters
   CV selection with Move. Separate face islands and holes get their own guides.
3. Change the point count with minus/plus, by typing, or with
   **Ctrl+Shift+Wheel**. The supported range is 3-32 points per curve.
4. Move CVs to shape the selected mesh region. The mesh updates during the drag,
   before mouse release. Moving a whole guide or its group also updates the mesh.
5. **Apply** bakes the result and removes the temporary guides. **Cancel** restores
   the original mesh and removes the guides.

Control Curve keeps its active mode while Ctrl/Shift are pressed or released.
**Ctrl+Shift+Wheel** changes curve points; a transient Shift-only wheel event
is consumed without changing mesh segments. Late segment-wheel/numeric events
from ReSegments cannot switch or resample an active Control Curve session.
Modifier release during panel startup cannot apply the preview. Explicitly
choose ReSegments to edit mesh counts again; Alt topology switching is limited
to that section. Existing canonical runtime code upgrades at a safe boundary
without rewriting Maya hotkey preferences or closing an active session.

**Save Curves** keeps the guides and their live connection to the mesh, then
closes Control Curve. Select their CVs with Maya's ordinary tools to continue
editing; the toolbox can be closed. Saved bindings work for edges, closed loops,
multiple chains and face contours, including smooth interior movement and holes.
They remain editable after saving/reopening `.ma` or `.mb` scenes with Model Pack
installed. Save and subsequent CV moves support native Undo/Redo. Several saved
bindings on one mesh remain connected when its topology is unchanged. Changing
upstream vertex topology bypasses the old binding safely; deleting construction
history bakes its current result, as with other Maya history tools.

**Align to Curve** appears below the control-point count for edge selections and
starts unchecked in each new preview. Enable it to fit the selected edge flow
directly onto its control curves, including before moving a CV. Subsequent CV
edits keep the edges on those curves. Unselected vertices, vertex/face counts,
connectivity, UVs and edge hardness are retained. Open-chain endpoints stay fixed
until their curve endpoints are edited. Disable the checkbox to restore the
original detail while keeping CV edits, including after changing the control
point count. Apply and Cancel use the same preview and native Undo/Redo.

With alignment off, creating curves preserves the current mesh shape and changing
their point count refits the cage without moving the mesh. With alignment on,
refitting updates the flow to the new curve. You can switch between ReSegments and
Control Curve while retaining the count, topology choice, curves and shape edits.
Click ReSegments to enable Shift+Wheel; Ctrl+Shift+Wheel shows the curve controls
while a ReCurve panel is active.

For faces, editing contour CVs smoothly moves both the boundary and interior
vertices of the selected region. Vertices outside that region stay in place;
faces sharing its boundary follow their shared vertices normally. Vertex/face
counts, connectivity, UV sets and edge hardness are retained. Face controls use
the existing point-count field, Ctrl+Shift+Wheel, Apply and Cancel. ReSegments
still requires edge selection. Select a surface patch with a boundary; a fully
closed selection has no contour and is rejected before changing the mesh.

Apply and Cancel end the preview while leaving its settings available. Click the
active bottom button again to collapse the settings and cancel an unfinished edit.
Switching to another Model Pack tool or closing Model Pack also cancels an
unfinished ReCurve preview. Maya Undo/Redo works after applying.

Switching to another application with Alt+Tab keeps the active ReSegments or
Control Curve preview. Segment count, topology mode, control-point count,
Align to Curve, CV edits and live Undo/Redo remain available when returning to
Maya. Application focus only resets temporary Alt/Space key state; it does not
apply or cancel the edit. Pending wheel and numeric changes are retained.

In ReSegments, **Ctrl+Z** steps back through live segment counts.
**Ctrl+Shift+Z** or **Ctrl+Y** steps forward. At the initial count, another Undo
cancels the preview. Text fields retain their normal text Undo. Curve CV moves
retain Maya's native Undo; segment Undo keeps the current curve shape. After
Apply, Maya's normal Undo restores the mesh and its previous input history.

The settings expand vertically; switching between ReSegments and Control Curve
retains the existing window width. Only the active function's wheel shortcut
appears beneath its button; both hints are hidden when the settings are closed.
The extra text between the settings and Apply/Cancel, and the shortcut captions
under Apply/Cancel, are removed. Full shortcut descriptions remain in tooltips.

## Shortcuts

| Shortcut | Action |
| --- | --- |
| **Shift+R** | Open ReCurve settings in Model Pack; retain the current section if already expanded. |
| **Shift+Wheel** | Increase/decrease segments only while ReSegments is active. |
| **Ctrl+Shift+Wheel** | In an active ReCurve panel, create/refit controls, change their point count and show Control Curve. |
| **Alt**, tapped alone | Switch N-gons / Triangles. Alt+mouse remains viewport navigation. |
| **Space / Enter** over the viewport | Apply the preview. |
| **Enter** in a number field | Confirm that field; in the curve field, create controls if needed. |
| **Esc** | Cancel the preview. |
| **Ctrl+Z** in ReSegments | Undo a live segment-count change. |
| **Ctrl+Shift+Z / Ctrl+Y** in ReSegments | Redo a live segment-count change. |

Plain wheel input remains viewport zoom. Another active Model Pack tool retains
its own input handling; Shift+R explicitly activates ReCurve. After closing Model
Pack, Shift+R opens ReCurve's standalone Mini Tower panel. The window stays fixed
and supports manual header dragging.

## Compatibility and preservation

The package uses Maya's bundled Python, PySide6, shiboken6 and NumPy. The supplied
Model Pack release targets Maya 2027. The ReCurve integration was checked with
native Maya geometry and offscreen Qt input in **Maya 2025 and Maya 2027**.
See VALIDATION.md for the actual coverage and limits.

The ReCurve engine retains named UV and vertex color sets, their current sets, object transforms,
and existing normal-lock rules. Ordinary unlocked normals stay automatic.
Non-manifold geometry is accepted without running mesh cleanup. Separate chains
must not branch or share selected vertices.
The evaluated geometry of skinned/deformed meshes can now be used as a snapshot.
Preview temporarily bypasses upstream inputs. Apply keeps the edited snapshot;
Cancel or Undo restores those original input connections, including animated
vertex tweaks. The operation does not delete upstream history nodes. Instances,
references, locked mesh inputs and multiple materials retain the
existing limits.

ReSegments transfers RGB/RGBA/alpha color sets through the same source face
corners as UVs, interpolating painted colors on newly inserted points. Color
seams, unpainted corners, empty sets, representation and clamping are retained.
Control Curve deforms the mesh without changing its colors. Apply, Cancel,
Undo/Redo and saved scene/curve bindings preserve this data.

Topology changes run through Maya's idle queue. A 33 ms main-thread timer updates
curve deformation and refreshes the active viewport during dragging. Large meshes
can update more slowly. Guides stay temporary unless **Save Curves** is used.

## Files

- `model_pack_recurve.py`: embedded controls, shared-session ownership, input
  routing, tool switching and safe panel cleanup.
- `recurve.py`: preview, Apply/Cancel, Undo/Redo and the existing shortcuts. This
  bundled copy adds host/input hooks, evaluated-history snapshot support and
  live segment-Undo notifications. The resampling algorithms are unchanged.
- `recurve_strip.py`, `recurve_bulk.py`: selected-chain topology assembly.
- `recurve_uv.py`, `recurve_colors.py`, `recurve_normals.py`: UV, color and normal preservation.
- `recurve_controls.py`: live curve binding, fitting and deformation.
- `recurve_panel.py`: standalone fallback panel.
- `install_recurve.py`: safe runtime updating, called automatically by Model Pack.
- `install_model_pack.py`, `launch_model_pack.py`: shelf installation and opening.

The original Model Pack tool modules and attribution remain included.
[UPSTREAM_README_RU.md](UPSTREAM_README_RU.md) preserves the supplied reference
for those tools; its original release/download links refer to the upstream build.

Line Sketch in Fast Draft: click successive points on a surface plane. Click the first point to close, Enter or Finish Sketch for an open curve. Angle step defaults to 15 degrees (0 disables); Parallel / Perpendicular follows drawn segments and explicitly selected reference edges within 4 degrees. Q/W/E/R preserves the clicked sketch as a native editable curve; Esc discards unfinished drawing. Finish then drag depth to extrude or cut. V/C/X remain momentary native snaps.

## Modelling tool updates

- Model Pack opens as a compact tab alongside Modeling Toolkit. Drag numeric labels horizontally with LMB; Shift adjusts more precisely and Ctrl adjusts faster.
- Mirror Cut and Mirror Dup use the world origin in World mode for both single and multiple selections. Local mode uses the pivot orientation (the active object's orientation for a shared plane). Mirror Cut automatically keeps the occupied side when all inputs lie strictly on one side on activation or when changing axis/space. Explicit direction changes remain available.
- Reset Tube: **Reduce loops by angle** uses the mean dihedral angle between face pairs on either side of each source loop. **Removal angle** is the maximum eligible angle; 0° removes straight redundant loops first. Similar nonzero angles (about 0.1° apart) prefer the shorter average sum of the two adjacent longitudinal spans. Neighbor angles and lengths are recalculated after each removal. End profiles, source stations and polygon winding are preserved. **Additional loops** subdivides retained bands. Disable reduction to retain all source loops. Shift + RMB changes sides; Ctrl + Shift + RMB adjusts the removal angle by 0.5° per step. All edits remain inside the Reset Tube Undo session.
- Path: select an initial component, hover another to preview the shortest path, then **LMB** selects the shown route. Shift + click adds/removes a path; Ctrl + Shift only adds. Hover picking uses mesh ray intersections and projected components without temporary Maya selections. Picking a Move/Rotate/Scale handle continues to transform the existing selection.
- Grow/Shrink support faces, edges and vertices. Ctrl/Shift choose broad/narrow directions on quad strips. Repeat Along/Across also supports vertex groups on quad grids, including closed routes.
- Fast Draft: Z / Ctrl+Z undo each committed draft; Shift+Z / Ctrl+Y redo it while the tool stays active. Numeric text fields retain their normal text editing shortcuts.

Undo sessions: Reset Tube, Mirror Dup, Fast Draft, Pencil Curve, Clone Slide, Slide Bevel, Plane Deform, Edge Flow and Hard Edges now collect their native commands into one session. While active, native Undo/Redo still steps through individual edits; finishing or closing the panel collapses the session into one undoable action. Reset Tube includes optional original deletion in this action. Offset, Quad Dome and Floater provide local Undo/Redo for preview settings and one native Undo after Apply; preview Undo never removes an earlier scene operation. Bevel and Floater Fit retain their existing stepwise preview/session histories.


### Grouped toolbox and Offset Inset

The dock keeps all 24 tools in four collapsible groups: Mesh, Edges, Curves, Surface.
Floater and Floater Fit stay next to each other. Tool commands and contextual menus are retained.

Offset supports connected or disconnected face selections on one mesh, including curved patches:
- **Offset**: inset width; negative values expand outward.
- **Offset Variation**: interpolates the width from full Offset at 0% to the original mesh at 100%, with or without Preserve Groups.
- **Subdivision**: intermediate inset contours (0 means one border strip).
- **Preserve Groups / Maximum Angle**: keep adjacent selected faces together below the face-normal angle threshold.
- **Keep Corner Radius**: optional original rounded-contour mode for a near-planar single region, without subdivisions.
  Disable it for general native Inset. Non-planar and split groups use general Inset automatically.

Numeric fields support label dragging and sliders. Preview settings undo step by step;
Apply commits the inset as one Maya Undo action. Cancel restores the source.
Inset can self-intersect at excessive widths; choose a width appropriate to the contour.



## ExtrudeX internal contour Inset

Select a closed internal edge contour around a filled, nearly planar region and click ExtrudeX. Offset now creates a new contour inside the original on the same mesh. Drag to change its inset; the original contour stays fixed and the new inner contour becomes selected. The inset stops before an original cap polygon reverses orientation. Shift-drag switches to normal extrusion; Q or another tool finishes the operation as one native Undo step. Open border edges continue to use the supplied ExtrudeX implementation. Internal contours must be closed, unbranched and identify a unique filled side. ExtrudeX uses the original Joe Wu runtime installed separately (see above).
