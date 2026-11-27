# Integration validation

## Slide Bevel concave corners and nested contact bands 0.1.71

The real failing selection was captured read-only after the user left its
source edges selected: 67 vertices, 36 faces (including concave 16/17-sided
caps), 73 selected edges, and 12 fully selected faces. The rail-only fix in
0.1.70 did not cover this topology.

Bevel's face guides now follow the actual inward miter direction, not a ray
toward the polygon centroid. A boundary rail that points outside a concave
face is replaced by an inward guide, anchored to the source endpoint when
constructing the cut. Interior miter edges stop and weld at their first
piecewise-linear contact, including simultaneous contacts. Fully selected
faces use world-space miter motion on zero-offset extrude topology, allowing
their short side to meet rather than leaving the old 99% gap.

The n-gon face-wide 49% clamp is removed from Slide Bevel. Individual guides
and corner contacts limit only the affected region. Generated crossed bands
between nested stages collapse their shortest generated adjacency to the older
contour, retaining source vertices and resolving contour selection after the
native weld. Tests distinguish actual crossings from the source mesh's float
position tolerance (about 0.0001 cm at this capture's object-space coordinates).

Multiple Bevel stages construct their cuts at a small offset before moving the
whole sequence. The direct command and live preview now share this process.
Miter guide refresh is specific to Bevel; Clone Slide retains its centroid
guides and previous fraction limits.

The 16 native/offscreen Slide checks include the captured selection with one
and two loops at widths 0.03, 0.15, 0.3 and 0.5, reverse dragging, closed
manifold/area/UV checks, no face crossings beyond native precision, and a
specific large-face point that must reach 0.3 rather than stop with the narrow
side. Captured result publication, Undo/Redo and MA/MB round trips also pass.
Mandatory Fast Draft checks and clean-process reloads run in Maya 2025/2027.

## Slide Bevel local rail joining 0.1.70

Opposing cuts on shared rails now meet exactly at the rail-local collision
distance. Uniform mode no longer uses a face-wide minimum span or the 49%
stop for those rails. Wider rails keep moving. Meeting vertices are selected
explicitly for native polyMergeVertex, which removes the collapsed strip and
leaves one contour. Single-sided cuts can also reach their rail endpoint.
Disjoint n-gon cuts without opposing rails retain their conservative limit;
Clone Slide keeps the original fraction-limit path.

Multiple cuts defer welding until all stages have moved, then weld their
intentional contacts together. Proportional inner cuts retain the original
rail scale after an outer cut stops on a narrow strip. Published geometry and
preview use the same merged contours. A welded disposable preview restores
its original mesh/materials and rebuilds before the next update, allowing
reverse dragging to reopen the loops. Source geometry/history and the native
result/Undo mechanism remain unchanged.

`.maya-tests/check_slide_collisions.py`: 14 native/offscreen checks cover narrow
and wide faces, different rail widths within one quad, repeated reverse drags,
counts/biases, proportional placement, closed thin plates with one welded
12-edge loop, branch corners, transforms/units, preview/direct parity, one-step
Undo/Redo, publication/reediting, source history/cancel, MA/MB and Delete History.
These checks and mandatory Fast Draft regressions run in Maya 2025 and 2027.
Fresh processes load the stored Slide Result and retain its welded wall loop.

## Conforming quad skirts and fixed lip supports 0.1.69

Support loops are now Opening Width / 8 from each lip, clamped to Skirt / 2
only for a very short skirt. They no longer move to the midpoint when the outer
band grows. This is shared by free and surface-bound ribbons; zero skirt keeps
the five-point groove. Surface tracing preserves these anchors before bounded
simplification, so distant skirt edits cannot move an existing support.

Surface offsets walk unfolded native mesh triangles at arc distances instead
of projecting smooth-normal offsets back to potentially different faces. A
crease start uses its incident faces/edge. Flat internal triangle diagonals do
not generate loops. Physical bends produce the minimal shared quad-column
count, while tiny bends below width/1000 (or coordinate tolerance) are reduced
without changing anchors. Every quad is checked for both triangle orientations,
in addition to the outer-rail checks. Open support borders reject an overrun;
an invalid edit retains the last valid mesh through the existing session/node.

The 22 surface checks include the user's captured 322-face support and 28-row
curve, widening both skirts with stable support positions and corner footprint;
a wide skirt crossing a beveled support with verified surface conformance and
arc length; flat triangulation and open-border behavior. The existing sphere,
cylinder, transforms, cm/mm/m, live history, MA/MB, Undo/Redo, plugin reload and
gesture checks remain. The 24 free Floater checks now verify fixed support
distance across skirt values, symmetry and the short/zero-skirt cases. Edge
conversion and mandatory Fast Draft checks run in Maya 2025 and 2027, followed
by fresh-process scene loads.

## Edge-derived Floater and skirt support loops 0.1.68

Both free and surface-bound floaters now add one longitudinal support loop
midway along each skirt, as requested. The five-point groove remains unchanged;
a nonzero skirt produces nine profile points, zero skirt keeps five. The new
points preserve skirt height, surface conformance and matching authored normals.
Curve-path row counts remain unchanged: the captured 12-corner contour now has
12 x 9 vertices / 12 x 8 quads, without extra longitudinal sampling. The updated
24 Floater and 19 surface checks verify midpoint position and unchanged height.

Starting Floater on mesh-edge components now validates every selected connected
chain/loop, then runs native polyToCurve (degree 1, best-guess form, no history,
no smooth-mesh-preview conformance) before starting the existing curve session.
Disconnected paths and multiple meshes are handled separately. Canonical
selection strings preserve mixed-component detection where Maya's getComponent
would otherwise expose only the first component kind. Branches and edge/face
mixtures are rejected before conversion; a later conversion failure rolls back
the whole native conversion chunk.

Converted curves retain a hidden support message and instance index. This also
allows edge selections on existing procedural floaters to create a new source,
without accidentally editing the old floater. Conversion is an independent
Undo step and survives Floater Cancel; Apply is the next Undo step. Native CV
edits between those steps, Undo/Redo, and saved curve data remain editable.

`.maya-tests/check_floater_edges.py`: 13 checks cover open/single/closed chains,
disconnected paths and multiple meshes, world transforms and cm/mm/m units,
instances, bad selections and transactional failure, staged Undo/Redo with CV
edits, scene round trips/Delete History, the actual toolbox button and edges on
existing floaters. Original mesh geometry/history are checked for isolation.
Existing surface, Floater, Auto Merge and mandatory Fast Draft regressions run
in Maya 2025 and Maya 2027. Fresh-process edge-derived scenes load and remain live.

## Floater Shift-based combinations 0.1.67

Width stays on Shift + LMB. Height now uses Shift + Ctrl + LMB, and skirt
uses Shift + Alt + LMB. Exact modifier matching leaves Ctrl + LMB and Alt + LMB
available to Maya and rejects extra three-modifier combinations. The panel and
documentation display the same bindings. Existing gesture checks exercise all
three mappings, reject the old mappings and keep release/focus/Esc behavior.
Floater and mandatory Fast Draft checks run in Maya 2025 and Maya 2027.

## Floater left-button gestures 0.1.66

Floater's viewport gestures now use Shift + LMB for width, Ctrl + LMB for
height and Alt + LMB for skirt. Press, move and release all use the left button;
modifier release, focus loss and Esc retain the same gesture lifecycle. The
former right-button context-menu suppression is removed. Native RMB behavior,
plain LMB and text-field interactions stay available.

Updated native/offscreen Floater gesture checks verify all three combinations,
release over another input, Esc/focus/modifier teardown, native context and Undo
isolation, and that RMB/context menus are not consumed. Existing Floater and
mandatory Fast Draft regressions run in Maya 2025 and 2027 before publishing.

## Floater topology and native Bevel corners 0.1.65

Floater no longer subdivides from smooth-normal angles inside physical faces,
nor doubles every interval of the groove profile. It retains curve samples,
locates transitions between noncoplanar support polygons on their shared edges,
and starts with the original five/seven-point cross-section. Cross-section counts
follow real support bends using the minimum shared count required for quads.
If exact edge crossings would fold the full-width ribbon near a mesh vertex,
only geometric projection error drives a bounded fallback; smooth normals still
do not add loops. Support and source data remain read-only.

The captured 13-CV degree-one polyToCurve contour has 12 unique corners. Its
floater now has exactly 12 rows x 7 profile points (84 vertices / 72 quads).
`.maya-tests/check_floater_surface.py`: 18 checks include this fixture, a flat
grid with changing custom normals, exact rows at real support bends and all
previous surface/gesture regressions.

New/re-edited Bevel sessions use native polyBevel3 Auto mitering with
Subdivide N-gons, matching the standard Maya performPolyBevel settings rather
than the additional all-quad junction pass. Shape changes no longer promote
one segment to three, including recovered bevels. Old saved forced-quad
histories retain their data until explicit re-edit; the old junction algorithm
remains covered separately through an internal compatibility argument.
Independent selected edges ending in an existing coplanar source-face fan
keep the unfilled flat cap, preserving the earlier no-floor-triangulation fix.

`.maya-tests/check_bevel_native.py`: six checks compare exact native topology
for full and partial selections at segment counts 1-6, shape/segment round trips,
uniform round positions, preview/Apply/Undo/Redo, Channel Box changes, explicit
legacy-history migration with Cancel/Undo and scene round trips/Delete History.
Existing edit, preview, history, smoothing, zero-width, Apply and legacy junction
regressions, plus Floater and mandatory Fast Draft checks, run before publishing
in Maya 2025 and 2027. Fresh-process surface and native Bevel scenes are checked.

## Surface-bound Floater and RMB gestures 0.1.64

An optional modelPackFloaterSurface node fits the quad ribbon to an evaluated
support mesh. The existing modelPackFloater node retains its schema and size
controls, so already-loaded Maya 2025 plugins do not need deregistration or a
restart. A dependency through the three size attributes connects the nodes;
there is no reverse message cycle. Delete History removes both nodes.

Surface projection starts from the nearest sheet and follows interpolated
face-vertex normals with local bidirectional ray refinement. This avoids jumps
between neighboring facets when a curve moves just beneath a curved mesh.
Adaptive longitudinal/cross-section sampling preserves quads and follows mesh
curvature. The skirts have the support's normals; Height offsets them along
those local normals. There is only a tiny numerical clearance at zero height.
World-mesh matrix metadata is baked into a private query copy under transforms;
authored face-vertex normals are transferred explicitly after point updates.
Source geometry, shading and history are read-only.

Explicit curve + mesh selection takes precedence. Otherwise tagged Fast Draft
targets and nearby visible non-floater meshes can supply the support. Multiple
equally close candidates require explicit selection. Free curves stay free
when no nearby support exists. Existing free floaters can acquire a support,
and bound floaters can be retargeted with reversible preview/Apply.

Shift/Ctrl/Alt + RMB drag controls width/height/skirt. A viewport-only event
filter consumes the owned press and marking-menu/camera gesture, updates the
existing fields, ends on RMB/modifier release or focus loss, and restores the
gesture start on Esc. It creates no Undo commands until the final Apply.
Text fields and unrelated input remain available; W/E/R still edit the curve.

`.maya-tests/check_floater_surface.py`: 15 native/offscreen checks cover planes,
spheres, a closed cylinder wrap, matching authored skirt normals, nonuniform
mirrored world transforms, automatic/ambiguous targets, live source and support
edits, scene round trips, Delete History, legacy upgrades and retargeting with
Cancel/Undo/Redo, plugin reload, scene units, deleted supports and RMB lifecycle.
The 23 existing Floater, Pencil Curve, Auto Merge and mandatory Fast Draft checks
also run in Maya 2025 and Maya 2027. Fresh-process surface-scene loading is checked
separately before publishing.

## Floater freehand-depth and startup error handling 0.1.63

The selected Pencil Curve fixture has 13 CVs and a shallow depth variation.
The former planar-only check failed, then the startup code caught every
ValueError and halved all sizes twelve times. Resampling at the resulting tiny
width hid the real failure behind a misleading curve-complexity warning.

Floater now retains each sample's original depth relative to the fitted main
plane. Its ribbon follows those depths, with normals derived from the local
longitudinal slope and rounded cross-section. Planar paths retain their prior
profile and normals. Offset dimensions remain in the fitted plane; intersecting
projections are rejected explicitly. Source CVs and curve history are unchanged.

Only dedicated OffsetError failures retry narrower starting sizes; each retry
samples at that actual width. Other path errors propagate immediately, and an
exhausted retry reports the offset failure itself. The sampling budget remains.

`.maya-tests/check_floater.py`: 23 native/offscreen checks include the captured
Pencil Curve, centerline fidelity at large coordinates, live CV edits and Undo,
nonplanar ribbon normals, early invalid-path errors and valid narrow-offset
retries. Floater and mandatory Fast Draft regressions run in Maya 2025 and 2027.

## Curve-driven Floater 0.1.62

One standard-width Floater button opens three three-decimal distance fields:
Opening Width, Height and Skirt. A curve-driven dependency node builds a separate
all-quad recessed ribbon. The source lies along the groove center/bottom, the
upper lips sit at Height, and flat skirts extend from both lips. The fixed
rounded profile has analytic normals matching the support plane at the lips,
valley and skirt boundaries. Planar UVs share corner indices across the closed
seam. Offset limits reject collapsed or intersecting ribbons before applying.

The node reads native NURBS data, including smooth/rational curves and Pencil
Curve output; degree-one paths keep their original corners. Sampling follows
the evaluated curve with a bounded chord error. World-space dimensions support
rotated, mirrored and nonuniformly scaled curves and cm/mm/m scenes. Initial
orientation uses the curve plane toward the view, then follows curve transforms.
Open planar paths produce open-ended ribbons. The base object remains unchanged.

New previews and edits to existing floaters are reversible without writing Undo
commands. Apply creates/re-edits native history in one chunk; Cancel restores
sizes without undoing independent source-curve edits. Source curves remain live
after Apply. The compact panel leaves Maya's W/E/R available, survives app
deactivation, handles Enter/Esc and other-tool/window/scene teardown, and blocks
Auto Merge during preview. Ordinary reloads update existing node methods.

`.maya-tests/check_floater.py`: 19 native/offscreen checks cover profile sizes,
trapezoids, concave and odd/reversed contours, zero skirts, open lines/arcs,
rejected inputs, flat authored normals, source/selection/Undo isolation,
Apply/Undo/Redo, live CV/parameter edits, mixed existing/new batches, renaming and
deleted inputs, invalid-preview recovery, no-op existing Apply, circle sampling,
UV closure, transformed curves and scene units, MA/MB/Delete History, plugin
reload and the actual panel/lifecycle controls. Clean-process scene loading is
checked by `.maya-tests/reopen_floater.py`. Existing Auto Merge, Quad Dome,
Offset, Pencil Curve and Fast Draft regressions run alongside these checks in
Maya 2025 and Maya 2027. An isolated Maya Software render checks the generated
trapezoid/circle meshes; `.maya-tests/floater_ui.png` records the compact panel.

## Auto Merge selection and repeat checks 0.1.61

Native polyMergeVertex selected its argument vertices, replacing edge/face
selection or appending vertices to object selection. Auto Merge now snapshots
the user's selection and interaction state, remaps selected vertex/edge/face
components onto the output topology and restores them inside the same Undo
chunk. Removed components are omitted. World-space positions support transformed
parents; object selection, other selected meshes, masks, hilite and context are
retained. Redo includes the selection restore. The existing idle/tool gates,
threshold, UV handling and native history behavior remain.

The service remembers the last evaluated mesh fingerprint and threshold, so
reselecting unchanged geometry cannot repeatedly add ineffective Merge nodes.
Geometry/threshold changes, re-enabling or a later selection after Undo permit
another attempt. Native Undo/Redo still pauses automatic rewriting.

`.maya-tests/check_auto_merge.py`: 28 checks include the original 20 plus exact
object selection through Undo/Redo, component identity without argument
selection, manipulator/mode restoration, a native HasNoEffect merge without
repeat history growth, explicit retry after Undo, an offline copy of the user's
471-vertex mesh, component remapping under a mirrored/nonuniform parent in
mm units, and retry after topology changes with unchanged points/counts.
Checked in Maya 2025 and Maya 2027 alongside Reset Tube and the
mandatory Fast Draft suite. Live scene diagnostics were read-only.

## Reset Tube n-gon caps 0.1.60

The persistent Reset Tube node and direct `resection()` entry point retain
the builder's one-polygon end caps. The previous post-build triangulation is
removed. UV welding, separate cap/body UV shells, soft body edges and hard cap
rims remain. Previously triangulated or center-fan ends are rebuilt without
their interior edges/center vertices; open ends and closed-loop tubes keep
their existing closure state.

`.maya-tests/check_reset_tube_caps.py`: 9 native/offscreen checks cover 3–256
sections including odd counts, repeated live count changes, triangle/fan source
caps, one/two open ends, closed loops, UV corner-coordinate equivalence,
manifold incidence, cap normals/rim shading, the direct script entry point,
source isolation, Undo/Redo, MA/MB history, Delete Original baking, upgrading a
loaded triangulating node, and the actual settings field/Shift+right-drag
gesture. Checked in Maya 2025 and Maya 2027 with the mandatory Fast Draft suite.

## Quad Bevel junctions 0.1.59

The data-only junction node replaces generated corner caps with harmonic quad
grids. Odd perimeters route a parity split through neighboring quad strips;
crossing routes divide a quad into four. Native flat/round templates remain
connected for compatibility, and old saved history opts into the new topology
only on explicit re-edit. Original vertices, unrelated source n-gons and flat
single-strip ends stay unchanged. New round samples follow a fitted sphere
when the corner supports one. Named UV/color sets, corner seams, authored
normal locks and material face groups are transferred to the new patch.
Inserted strip samples interpolate the motion of their parent corners,
including independent local widths. Repeated closing vertices emitted by
native Bevel are normalized before rebuilding, retaining the face and its UVs.

`.maya-tests/check_bevel_junctions.py`: 12 checks cover 1–9 segments including odd counts,
the user's four baked 9-gon junctions, partial branches, width/profile/segment
changes, transformed meshes and transverse cuts, source isolation,
preview/Apply/Undo/Redo, multiple UV sets, colors/normal locks, saved MA/MB
history, re-edit/Delete History, legacy opt-in and loaded-helper reload.
`.maya-tests/reopen_bevel_junctions.py` checks clean-process scene plugin loading.
Existing smoothing, preview, edit, Apply, history and zero-width suites and
the mandatory Fast Draft runner are also run in Maya 2025 and Maya 2027.

## Signed Quad Dome height 0.1.58

Negative Height reflects the quad cap into the selected surface without
reversing its polygon winding. Analytic normals point toward the cavity;
the lip is hard and its outside face normals are not overwritten. Positive
domes retain their existing shared-rim behavior. A zero-height preview removes
the temporary mesh and restores the original selection/surface without a split
on odd side counts, a singular normal calculation, or an empty Undo operation.
The three-decimal Height field and mouse dragging accept both signs.

`.maya-tests/check_quad_dome.py`: 21 native/offscreen checks include the prior
15 checks plus signed point reflection and normals on 3–23 sides, recess lip
and unchanged outer normals, repeated zero crossings, no-op zero Apply,
negative Cancel/Apply/Undo/Redo and MA geometry/normal persistence, bottom faces
under a mirrored/rotated/nonuniform parent, and the actual negative Height field,
mouse gesture and Apply button. Checked in Maya 2025 and Maya 2027 alongside
the mandatory Fast Draft regression runner.

## Quad Dome 0.1.57

Native all-quad rounded caps use latitude bands and a harmonic rectangular
crown, with a three-quad valence-three crown for six-sided boundaries. Odd rims
add one ellipse/circle sample and propagate a split across the adjacent quad
strip. Original vertices remain fixed; the opposite original n-gon only gains
the required section. Caps use analytic ellipsoid normals, shared with rim
side corners, while unrelated smoothing and authored normal locks remain.

`.maya-tests/check_quad_dome.py`: 15 native/offscreen checks cover 3–32 source
sides, odd/even all-quad caps and side strips, manifold incidence, no orphan
vertices, face winding, height/ring extremes, exact circular/elliptical equations,
normal continuity, reversed-facing bottom caps, mirrored/nonuniform/rotated
parents, cm/mm/m units, triangulated source caps, rim-only mode, original-source
and Undo-queue isolation, Apply/Cancel/Undo/Redo/Delete History, multiple UVs,
colors, two materials and MA/MB geometry/normal round trips. Actual compact UI,
keyboard and tool/window switching, app deactivation and Auto Merge exclusion
are covered. Checked in Maya 2025 and Maya 2027 alongside existing Offset,
Auto Merge, ReCurve color and Fast Draft suites.

## ReCurve vertex colors 0.1.56

ReSegments and Control Curve now accept painted meshes. The general, circular,
group-strip and bulk topology paths transfer named face-vertex color sets using
their original corner correspondence, retaining RGB/RGBA/alpha representation,
clamping, current set, empty/sparse sets and independent colors across seams.
New colors interpolate only between two painted endpoints. Quad-strip end
corners use the source face on the correct side of an existing color/UV seam.

The owned intermediate mesh source also writes Maya's native color plugs for
scene persistence. Before/After data remain independent of those node writes.
Save Curves only reconnects original upstream history when its color data also
matches the evaluated preview; otherwise the colored snapshot remains its source.

`.maya-tests/check_recurve_colors.py`: 17 native/offscreen checks cover all
topology paths, increasing/decreasing counts, n-gons/triangles, sparse and empty
sets, seam interpolation, a 20,164-face bulk mesh, source/selection cleanup,
multiple UV sets, runtime upgrading without preference reinstall, Apply/Cancel/Undo/Redo, repeated source reuse, edge and face
controls, Save Curves, upstream color differences and MA/MB scene round trips.
Checked in Maya 2025 and Maya 2027 alongside existing Control Curve, saved-curve,
focus/shortcut and Fast Draft regression suites.

## Ordinary Maya/Pencil curves in Fast Draft 0.1.54

Curve discovery now accepts ordinary NURBS transforms, shapes and selected CVs,
without creating a Model Pack pattern tag or replacing their native history.
The Scene Curves chooser includes these curves and identifies individual shapes
when a transform owns several curves. Ambiguous multi-curve/mesh selections
fail before geometry edits. Existing saved v1 patterns retain their closed
contour behavior and native primitive compatibility.

Open profiles sample the evaluated curve in 3D and extrude only its side quads,
using an undoable native plane grid and native vertex placement. No endpoint
connection or cap is added. Either depth sign creates a wall, including on a
two-point line, rational arc, smooth Pencil stroke or nonplanar open curve.
Closed planar profiles use the existing solid/n-gon and Boolean paths. Target
selection accepts curve + one mesh; curve-only sessions can pick a target under
the depth gesture. The target normal orients the closed contour. A successful
cut adds only a target message link to the original curve, follows the visible
result on reuse, and includes that link in the native Apply Undo group.

`.maya-tests/check_draft_scene_curves.py`: 15 native/offscreen checks cover
scene discovery, transform/shape/CV selection, individual multi-shape curves,
intermediate original shapes,
open planar/nonplanar/rational/straight curves, positive/negative depths in
cm/mm/m, real Pencil open and closed results, native generator history, source
preservation, save/reopen and Undo/Redo, targetless/invalid rejection, actual
ray-picked cut targets, explicit-target precedence, multiple targets, existing
primitive defaults, Scene Curves activation and Esc/native-manipulator exits.
Regression includes the 21 saved-pattern checks, 10 edited-profile checks,
Pencil Curve checks and the required 115 Draft checks (two expected upstream
UI skips), using isolated Maya 2025 and Maya 2027.

## Mirror Dup viewport axis points 0.1.53

The three existing midpoint controls are now available in Mirror Dup as well
as Mirror Cut. Duplicate point clicks select X/Y/Z without toggling the shared
cut-direction preference. Clicking the active duplicate axis consumes the click
without adding an Undo action. Duplicate tooltips describe axis selection;
the Cut controls retain their side reversal and positive/negative appearance.

Four additions to `.maya-tests/check_shared_mirror.py` exercise actual projected
control picking, all-axis switching on shared World/Local frames with transformed
parents, a single source with a moved/rotated local plane, unchanged sources and
duplicate identities, no-op active-axis clicks, grouped native Undo/Redo, and
repeated-click Cut side reversal. Relevant checks run alongside required Draft
regressions on isolated Maya 2025 and Maya 2027.

## Fast Draft from edited native curves 0.1.52

Saved pattern contours are now read from actual world-space NURBS geometry,
including native CV edits, rebuilt degree/knot data, transforms, negative scale
and frozen transforms. Degree-one curves retain their contour vertices. Smooth
curves are sampled adaptively against their evaluated knot spans at 0.05% of
their control bounds. Nonplanar, degenerate and self-intersecting contours fail
before geometry edits. Existing v1 saved patterns remain supported; new linear
patterns are periodic so native seam-CV edits keep them closed.

`curve_profile.py` supplies the same planar ring to the transient preview and
native polygon creation/extrusion/border closing commands. Concave outlines
have one n-gon per cap and side quads, without center vertices. Native unchanged
box/cylinder contours retain the existing creation and cylinder cap parameters.
Edited cylinder wall edges are softened without changing unrelated target
edges. Cut metadata can also retain and copy arbitrary edited contour shapes.

Depth reuse never replaces the source curve's CVs, weights, knots, degree,
history, UUID or transform. Q/W/E/R returns to Maya curve editing; selecting a
saved curve or its CVs and reopening Fast Draft resumes its actual boundary.
The saved-pattern chooser resumes immediately. Edit Pattern, Rotate, Align to
Edge and the obsolete alignment option were removed from the visible UI/menu.

`.maya-tests/check_draft_curve_profiles.py`: 10 native/offscreen checks cover
edited box/cylinder CVs with rotations/negative scale/frozen transforms in
cm/mm/m, concavity, n-gon cut floors, wall smoothing, copied cut contours,
sampled cubic rebuilds, preserved native curve generator history, invalid
contours, native Undo/Redo, scene save/reopen, repeated edits, selection of CVs,
Q/W/E/R/Esc and the compact panel. Regression includes the 21 pattern workflow
checks and the required 115 Draft checks (two expected upstream UI skips), on
isolated Maya 2025 and 2027.

## Whole-group polygon and edge repetition 0.1.51

The Select periodic buttons now repeat the complete connected input patch.
The period is its width in the selected direction plus the configured gap;
this replaces thinning the selected polygon patch in the UI. Single seeds keep
their every-N behavior. The older `core.periodic` API remains available unchanged.

`model_pack_selection/repeat.py` propagates oriented quad-lattice states and
patch coordinates, with cached open/closed routes. Each translated copy must
retain the complete patch's adjacency and orientation; boundary/pole crossings
cannot leave partial rows. Torus seams and full cross-section selections are
handled as periodic coordinates, with a single traversal rather than repeatedly
wrapping a coprime step. Original selected components are always retained.

Edges use connected edge-loop continuation at regular vertices and opposite
edges across quads for edge rings. Parallel edge rows, chains and complete loops
repeat as groups. Mixed polygon/edge selections are detected from serialized
component ranges because Maya can store both kinds in one selection-list entry.
All targets preflight before a native face/edge selection command. No geometry,
UV, smoothing or source history is edited; Undo/Redo is one selection step.

`.maya-tests/check_selection_repeat.py`: 16 native/offscreen checks include
2D rectangular/L-shaped groups, three-wide periods, seam-crossing groups,
complete cross sections, the screenshot-style 64-by-12 torus with gap 19,
open-boundary whole copies, both edge families/directions, edge chains/parallel
groups/whole loops, poles, native Undo/Redo, multiple meshes, invalid or mixed
input, preflight failure on a second mesh and the actual Select panel. Visual
checks confirm the generic gap label fits the unchanged panel width. Regression
includes Selection Path and required Draft on isolated Maya 2025 and 2027.

## Select Path on held Shift and one click 0.1.50

The mode now completes a native Shift single pick after release, including
Maya handlers that select on release. It supports held-Shift sequences and
flushes a completed click before capturing the next anchor. Rapid Qt double
events are handled as successive single clicks without invoking Maya's loop
selection. Plain picking, marquee, Alt navigation and native manipulator drags
remain native. Turning Path off restores normal Shift picking immediately.

Select's native endpoint and final path share one Undo chunk; there is no
automatic undo/replay of a previous native click. Move/Rotate/Scale keep their
native Undo ownership and remain supported after the toolbox idle timer runs.
Gesture identities protect deferred release callbacks; outside releases and
mode/context changes close pending chunks without applying stale paths.

Viewport matching now recognizes child native QWindows and wrapped containers
while checking the top widget so floating panels cannot trigger a path behind
themselves. Picker coordinates use the clicked view's pixel dimensions. The
toggle tooltip and README describe the required active Path mode and Shift-click.

`.maya-tests/check_selection_path.py`: 35 native/offscreen checks cover the
existing shortest vertex/edge/face paths plus release-time picking, held and
rapid Shift sequences, add/remove and Ctrl+Shift, one-step Select Undo/Redo,
remembered anchors, native render-window routing/overlays, correct view pixels,
manipulator drags and idle retention, outside releases, cancellation/stale
timers, preserved masks/preferences/geometry and disconnected shells. Regression
includes required Draft, Bevel Apply and Offset on isolated Maya 2025/2027.

## Slightly warped Offset input 0.1.49

The user's selected extracted face was captured read-only into
`.maya-tests/fixtures/OffsetUser0149.json`. Its 12-point border has a small twist:
the former plane check measured up to 0.031 cm at a 22.45 cm diagonal and rejected
the face before creating a preview. This reproduces the reported failure in an
isolated Maya session.

The support frame now passes through the contour center. Small normal-direction
variations (up to 0.5% of the maximum contour distance from its center) are
retained for each vertex. Offset acts in the support plane and adds back that
unchanged normal coordinate. Every recognized rounded corner still receives one
rigid 3D translation, preserving its original shape and pairwise distances.
Strong bends, projection crossings and degenerate inputs remain rejected.

`.maya-tests/check_offset.py`: 16 checks include the captured face's two rounded
corners, zero-offset reconstruction, unchanged vertex heights during repeated
preview changes, source/UV preservation, Apply/Undo/Redo, history deletion and
native scene save/reopen. A separate synthetic warped contour and mirrored,
nonuniform parent check the general behavior independently of the captured mesh.
Regression includes all previous Offset cases and the required Draft suite in
isolated Maya 2025 and 2027 application/temp directories.

Validation results: Offset 16/16 and Draft 115 (two expected skips) pass on both
versions. Maya 2027's existing Copy Cut without history check failed in two
earlier full Draft runs (missing ray hit/incorrect Boolean volume); the unchanged
full runner passed on confirmation. An inert-Offset control also passed. These
logs are retained as `check_draft_2027_0149_final.log`, `_retry.log`, `_control.log`
and `_confirm.log`; no Draft production code was changed for this Offset fix.

## Radius-preserving Offset 0.1.48

The requested radius rule is explicit: each recognized rounded corner retains
its original radius and samples. Inward offset translates the entire corner by
the intersection change of its two tangent support lines. It does not scale the
arc or decrease its radius. Tangent-triangle recognition supports nonuniformly
scaled corners; circle/tangent fitting also handles very short straight sides.
Sharp corners use shifted-line intersections. An area, edge-direction,
intersection and containment check limits the inset before collapse.

The one-face planar operation has a private, reversible preview. Original mesh
geometry/UVs and undo state stay untouched during adjustment and cancellation.
Apply uses native polyExtrudeFacet plus undoable component moves; it remains
valid after history deletion. Offset shares the normal main-button width and
bottom settings area, with a three-decimal field, LMB drag, Enter/Apply, Esc/Cancel
and Q/W/E/R exit. Application deactivation retains the preview. Normal tool
switches apply; window/scenes/reload teardown cancels; Auto Merge pauses while
Offset owns the tool. The update bridge waits for the active panel as usual.

`.maya-tests/check_offset.py`: 14 native/offscreen checks cover circle radii and
sample counts in both windings, mixed/sharp/collinear corners, short straight
sides with large radii, concave contours, real Maya Bevel samples, collision
limits, invalid input and external edits, transformed parents, mm/cm units,
UV/geometry parity between preview/Apply/Undo/Redo/Delete History, cancellation,
the actual panel, LMB drag, native tool keys, idle timer/focus retention,
Auto Merge exclusion and failed Apply retaining the user's preceding action.
Regression includes Draft, Pencil Curve, Auto Merge,
Bevel Apply and Bevel preview on Maya 2025 and 2027 in isolated app/temp folders.

## Bevel preview/apply parity 0.1.47

The native discrepancy is reproducible on a subdivided mirrored beam: querying
the original mesh's triangulation while the private preview exists changes the
later native bevel offsets. The preview duplicate and the original can therefore
choose opposite directions despite identical point coordinates and parameters.
The focused reduction is in `.maya-tests/probe_bevel_reads.py`; the full
regression suite is `.maya-tests/check_bevel_preview.py`.

Both native flat/round templates now receive the same clean mesh data through
an owned native polyTweak with an explicit zero displacement. Source geometry,
UVs and normals retain their values. The baseMesh input stays upstream of that
helper, so re-editing and zero-width removal can restore the original base.
The templateSource message records ownership for replacement and cancellation;
the helper participates in normal history deletion and native Undo/Redo. Saved
histories without this metadata upgrade on the next edit.

Seven native checks include 36 combinations of new, saved and recovered Bevel,
mirrored/nonuniform parent transforms, equal/independent widths, and input
tessellation before and after starting the preview. Width/profile drags and
segment round trips compare world points, polygon/triangle connectivity,
smoothing and UVs against Apply, Undo and Redo. Additional checks cover contact
limits, untouched source/UVs/unrelated helpers, owned-node cleanup, history
deletion and upgrading older histories. The regression set includes the existing
Apply, edit, smoothing, history, zero-width and required Fast Draft runners,
using isolated Maya 2025/2027 application and temporary directories.

## Control Curve shortcut isolation 0.1.46

The false ReSegments switch was reproduced by queuing the Control Curve request
before a late segment-wheel dispatch. The wheel previously resampled the shared
session, and count synchronization inferred that the UI should become ReSegments.
A separate opening-panel race allowed Shift release to apply a newly created
embedded preview. Late edits to the hidden mesh-count field had the same mode
switching effect.

Synchronization no longer chooses the active tool. Embedded section changes
invalidate queued field edits; segment-only count/topology callbacks are gated
by the explicit ReSegments section. The listener routes Ctrl+Shift wheel to
control points, consumes Shift-only transitions in Control Curve, and ignores
Alt/Meta navigation. The segment dispatch itself also checks the active section,
including while temporarily hidden. Embedded Shift release cannot auto-apply.
Standalone ReCurve's original no-panel Shift gesture remains available.

A routing capability check upgrades an older canonical loaded runtime without
reinstalling hotkeys. Safe reload refuses an active session. The fixed update
bridge still waits for that session; it is not cancelled to force a release.

`.maya-tests/check_recurve_shortcuts.py`: 10 native/offscreen checks cover both
modifier orders, Ctrl+Shift wheel and CV edits, fractional/Shift-only scroll
transitions over viewport/fields, queued segment wheel and hidden count edits,
count synchronization/Alt isolation, startup Shift release, explicit ReSegments
wheel behavior, safe runtime upgrade without preference writes, and retention
of an active Control Curve session. Regression includes ReCurve focus,
alignment, face controls, saved curves and the required Draft suite on Maya
2025 and 2027, with isolated application/temp folders.

## Select Path native Shift-double-click mode 0.1.45

Path is checkable in the Select panel/context menu. It observes the native first
click and intercepts only Shift-double-click in mesh component selections.
Ordinary click/toggle, Ctrl, marquee, Alt and unrelated widgets stay native.
Shift adds/removes the route according to the destination's pre-click state;
Ctrl+Shift always adds, including already selected endpoints. That case uses
Maya's own screen picker temporarily with selection/Undo restored in finally.
The endpoint anchor follows native selection changes and has a small selection
state cache for Undo/Redo. Existing Track Selection Order is read when available;
no selection preferences are changed.

In Select, a uniquely named selection-only chunk identifies the native first
click. Only that verified chunk may be folded into the final path selection, so
one Undo restores the seed. An intervening command or manipulator context is
never undone. Deferred click closure is token-scoped to avoid closing a later
gesture's chunk. Successful paths use native selectCommand and preserve unrelated
selection, masks, hilite, constraint settings, geometry and history. Starting
another function, closing the panel/window, or new/open removes the filter and
callbacks. The direct two-endpoint API remains compatible.

`.maya-tests/check_selection_path.py`: 26 native/offscreen checks cover existing
weighted paths plus real checkable panel/menu, Shift add/remove, Ctrl+Shift,
unchanged endpoints/native pick restoration, first-click passthrough, all three
component types, chained anchors/Undo/Redo, modifiers/marquee, manipulator and
intervening-command Undo protection, delayed closure, disconnected fallback,
mask/constraint/selection/geometry preservation and lifecycle cleanup. The
required Draft regression and Bevel Apply/Pencil Curve integration suites also
run in isolated Maya 2025 and 2027 environments.

## Pencil Curve 0.1.44

The new standard-width toolbox button owns a draggerContext and input-transparent
turquoise stroke overlay. No raw-stroke scene curve or Undo command is created
during dragging. Release creates one native Undo chunk. An adaptive cubic fit
is reduced by testing lower span counts against raw samples and a stroke
polyline; a straight stroke reduces to two CVs. Sparse corners retain their exact
polyline if a smooth fit would distort them. Closed strokes start periodic and
are reduced in that form, avoiding a closeCurve seam/blend artifact. Temporary
sources/candidates and fitting history are removed on completion; a command
failure rolls back the complete stroke. Pixel tolerance uses the initial view
scale and HiDPI ratio, independent of Maya's scene linear unit.

Plane modes cover view, picked mesh and world axes. Surface drawing stops at a
gap and cannot bridge a later hit. A pending stroke is cancelled on Esc, tool
change, scene transition and close; completed curves survive. Native Q/W/E/R,
numeric input, other Model Pack panels, Edit Points and safe update deferral
follow the existing toolbox lifecycle. Plane/tolerance/auto-close settings are
remembered. The bridge module and Maya preferences/command ports are untouched.

`.maya-tests/check_pencil_curve.py`: 17 native/offscreen checks cover two-CV lines,
jitter, cubic S/circle/corner/self-crossing shapes and fitting error, tolerance
versus CV count, periodic file roundtrip, sparse turns, cm/mm/m and tilted
coordinates, Undo/Redo and injected failure, invalid input, actual toolbox/panel,
release/next-stroke independence, cancellation and late mouse release, native
CV editing, window/tool exit, mesh-boundary gaps, scene changes, edge-on planes,
numeric typing and application focus. Required Fast Draft regression and related
persistent-pattern/Edge Flow suites run on Maya 2025 and 2027.

## Bevel end-cap topology and 60-degree boundaries 0.1.43

The end-of-strip artifact was reproduced on the vertical corner of a raised
plane face. Native fillNgons=True subdivided the new flat end cap: at three
segments it created 30 vertices, 49 edges and 20 faces. Using fillNgons=False
on both templates retains one six-sided cap and produces 28 vertices, 44 edges
and 17 faces, with the actual bevel strip retained. No mesh-wide dissolve or
merge is applied; existing saved native templates are not retopologized during
reload. Editing/creating a new template uses the new cap setting.

New strip boundary smoothing uses the angle between deformed world-space face
normals with a 60-degree threshold. Longitudinal strip-to-strip joins remain
soft for multiple segments. Flat generated end caps are identified separately
from longitudinal strips, so a 90-degree strip/cap border stays hard. The rule
runs for single segments too, and recomputes after width/profile changes.
Normal unlock is restricted to affected face corners. Maya's unlock operation
can harden neighboring edges, so native flags are restored before the local
overrides; unrelated geometry retains its smoothing. A small angular tolerance
stabilizes exactly 60-degree corners against float mesh storage.

`.maya-tests/check_bevel_smoothing.py`: 12 native checks include end-cap N-gons
for 2/3/6 segments, manifold incidence, source/preview/commit parity and Undo,
boundaries at 59/60/61/70 degrees, profile-dependent soft/hard transitions without
topology changes, nonuniform parents and mm units, locked normals, untouched
edge flags/UVs, saved/history deletion, hot reload, and existing subdivided-cap
compatibility. Related Bevel apply/edit/history/zero-width suites and the required
Fast Draft regression run in isolated Maya 2025 and 2027 environments.

## Edge Flow Parallel 0.1.42

The bottom panel adds Set Reference and Parallel. References retain mesh handles,
edge IDs and a topology signature. Apply reads current world-space directions;
multiple reference edges must share one direction. Reference edges are excluded
from target changes, and their shared vertices anchor adjacent selected chains.
Isolated target edges preserve midpoint and length. Open target chains preserve
every edge length and their vertex centroid unless anchored to the reference.
Closed/branched paths and incompatible anchors fail before writes on any mesh.
The existing transactional writer preserves edge selection and makes the click
one native Undo step, rolling back all meshes on a command failure. A no-op adds
no Undo command. No Maya selection preference is modified; the last-selected-edge
shortcut is available only with existing Track Selection Order enabled.

`.maya-tests/check_edge_flow.py`: 29 native/offscreen checks, including ten new
Parallel checks for separate edges, open paths, shared reference vertices,
multiple references, transforms and cm/mm/m, rename/reparent, stale/deleted
references, ambiguous/closed/branched selections, ordered-selection shortcut,
atomic multi-mesh failure, centroid/length preservation, UV/material/topology/
smoothing retention, Undo/Redo, and the actual panel capture/apply lifecycle.
Maya 2025 and 2027 also run the required 115-check Fast Draft regression suite
(two upstream standalone UI/plugin checks intentionally skipped).

## Persistent Fast Draft patterns 0.1.41

Completed bases persist as degree-one closed outlines made from native NURBS
curves with transform-center pivots and versioned pattern metadata. Their target
is a Maya message connection, not a stored path. The active tool owns the base
until release; Q/W/E/R/finalize/shape change/window close preserve completed
bases, while Esc and incomplete gestures discard the transient preview. Scene
new/open callbacks cancel the pending tool without saving into a closing scene.
Add/Cut and pattern creation share one Undo chunk. A Cut retargets the contour
to the visible result. Native transforms are read back when resuming depth.

Align to Edge rotates the nearest actual boundary segment (including cylinder
polygon sides). Its default projects the reference edge into the drawing plane;
the optional 3D mode uses a minimum quaternion rotation. Translation and scale
are retained. Ambiguous nonparallel selections are rejected before alignment.
The explicit pattern chooser preserves the reference-edge selection, including
when a different pattern remains selected. The bottom panel retains the existing
toolbox width; the same actions are available from the Fast Draft context menu.

`.maya-tests/check_draft_patterns.py`: 21 native/offscreen checks on Maya 2025
and 2027 cover world-space contour/primitive agreement for cm/mm/m and tilted
planes, native move/rotate/nonuniform scale, odd cylinder side counts, target
rename/reparent/deletion, .ma scene roundtrip, replace-in-place Undo/Redo,
unchanged-exit Undo stability, plane/3D alignment, parallel/ambiguous edges,
chosen-pattern routing, Q/W/E/R and late releases, Esc/incomplete gestures,
finalize/shape change/window close, UI actions, Add Undo and Cut target reuse.
The existing `.maya-tests/check_draft.py` suite passes with 115 checks (two
upstream standalone UI/plugin checks intentionally skipped) on both versions.

## Bevel Enter commit from mesh-shape selections 0.1.40

The reported rollback was reproduced with Auto Merge disabled. Edges selected
through the API/Select Path can be named on the mesh shape rather than its
parent transform. Bevel's preview duplicate was a transform and evaluated
correctly, but committing rebuilt on the original shape path. The helper queried
child shapes of that shape and indexed a None result; the commit exception then
undid the native Bevel chunk after removing the preview. A simple cube reproduced
12 preview vertices returning to 8 on Enter with a NoneType warning.

The Bevel shape resolver now accepts a mesh shape directly, and filters a
transform's children to visible mesh shapes. No selection/global preference,
Auto Merge threshold, or unrelated tool behavior is changed.

`.maya-tests/check_bevel_apply.py` adds six native/offscreen checks: Enter and
keypad Enter for transform/shape endpoint selections with native Undo/Redo;
finalize after Enter; ordinary tool switching; shape-path tool switching; new
Bevel after actual Select Path; and editing saved history from shape-face
selection. Source/preview/result topology and positions must agree. Regression
logs use _0140 for Maya 2025 and Maya 2027, including existing Bevel edit/history,
Auto Merge and Draft checks. Diagnostic inspection of the user's running Maya
was read-only; all geometry fixtures ran in isolated application/temp folders.

## Window-scoped active-mesh Auto Merge 0.1.39

One standard-width Auto Merge toggle is added to the main grid. It is independent
of the exclusive tool/panel state and starts off for each new window. As clarified
by the user, checks cover the whole active mesh on selection, including component
selection, rather than only selected vertices or all scene objects. A right-click
Distance (cm) field has three decimals and defaults to 0.001 cm. Physical world
distance is converted to native scene units for the merge command.

Enabled services listen for selection, geometry dirtiness and world-matrix
changes, coalescing them through a 200 ms timer and idle debounce. Spatial hashing
finds vertices with a neighbor in range; unchanged/no-pair results create no
history command. Native polyMergeVertex preserves history and uses texture=False
to keep UV seams, without forcing distant two-vertex merges. Component renumbering
and Undo/Redo remain native. Undo/Redo callbacks remember the restored geometry,
preventing an automatic remerge or loss of the Redo queue. The update bridge's
existing active-tool gate is reserved only during the native merge write.

Mouse gestures, popups/text edits, playback, disabled Undo, hidden/minimized
windows, application deactivation, nonstandard construction contexts and live
Model Pack editing sessions defer pending work. Instance/reference/locked meshes
are skipped. Closing the window or disabling the button stops its timer and
removes all mesh, scene and event callbacks; new/open scene transitions unbind
old geometry. No new command port, preference installation, recurring app task or
unrelated-scene scan is used.

`.maya-tests/check_auto_merge.py` contains 20 native/offscreen checks covering
spatial hashing against independent pair distances; active-only scope; complete
mesh checks from component selection; repeated geometry/transform callbacks on
the same mesh; cm/mm/m and scaled/rotated parents; UV seams/materials/upstream
history; native component remapping; no-op behavior; Undo/Redo with the real timer
running; window, disabled and new-scene lifetimes; input/live-tool/focus gates;
native history-off behavior; the actual toggle/settings UI; preserving an actual
Control Curve session; and live saved-curve history upstream of a merge. Logs
use _0139 for Maya 2025 and Maya 2027, alongside Select Path and Draft regressions.

## Select Path for vertices, edges and faces 0.1.38

Path is available beside the region action in the embedded Select panel and
in its context menu. It requires exactly two components of one type on one mesh,
includes both endpoints and selects the result in that same component mode.
Dijkstra traversal uses world-space edge lengths for vertices, endpoint-to-edge
midpoint distances for connected edges, and face-center/shared-edge-midpoint
distances for adjacent polygons. It follows actual topology, supports triangle,
quad and N-gon meshes, and cannot jump between disconnected shells or faces
which only touch at a corner. Equal-length choices are repeatable without
requiring Maya's ordered-selection preference.

All input and connectivity checks run before changing selection or context.
Selection uses the existing native chunk with a component-kind argument; its
default face behavior is retained for region/growth/periodic actions. No new
history nodes, constraints, preference changes or plugin commands are introduced.

`.maya-tests/check_selection_path.py` has 13 native/offscreen checks covering
weighted paths that use more shorter edges, adjacent endpoints, face-edge
adjacency, closed-ring choices, transforms and cm/mm/m units, triangle/N-gon and
nonmanifold connectivity, disconnected components, invalid/mixed/cross-mesh
inputs, unchanged geometry/UVs/smoothing/history, active selection constraints,
native Undo/Redo, the existing selection actions and real panel/context-menu
buttons. The panel keeps its width and height. Logs use _0138 for Maya 2025 and
Maya 2027; the full Draft integration runner is also checked in isolation.

## Save Curves persistent Control Curve bindings 0.1.37

The embedded Control Curve panel adds a full-width Save Curves action, hidden in
ReSegments and disabled without live controls. It finishes the preview while
retaining its NURBS guides and a scene-owned modelPackControlCurve history node.
The node evaluates curve data/matrices through normal Maya dependencies, with
no Qt timer, open panel or active Recurve session. Its stored baseline samples
and harmonic face weights match the existing preview, including Align to Curve,
refitted cages, multiple/closed chains and contours around holes. Matrix inputs
follow renamed/transformed guides and common parents. Upstream topology changes
pass through instead of addressing stale vertex IDs; missing guides are ignored
without using Maya's cached disconnected NURBS data.

Save adopts the binding through the existing native undoable Recurve command,
preserving its source/tweak restoration. Matching upstream topology retains
earlier history and saved curve sets; stored local offsets include pre-existing
tweaks. Resegmented topology uses the preview's mesh source. The current preview
is checked before closing: failed setup rolls back and leaves the live editor
available. Ordinary Apply/Cancel still remove their temporary controls.

`.maya-tests/check_saved_curves.py` contains 16 native/offscreen regressions:
post-exit edits, preview parity, selected-only changes, unchanged/static meshes,
known uniform translation, closed/multiple chains, face contours with a hole,
alignment/refitting/resampling, save/CV Undo/Redo, large transformed parents and
mm units, curve rename/deletion/Undo, two saved bindings on one mesh, reused
sources and runtime reload, upstream modeling changes and safe topology bypass,
failure rollback, serial/parallel evaluation, native history baking, and the
actual embedded button/toolbox close. Fresh isolated
processes reopen both Maya ASCII and binary scenes, autoload the new history
plugin through the package search path, and edit CVs without opening the toolbox.
Logs use _0137. Existing Draft, face controls, alignment and focus suites cover
the unchanged temporary-session behavior.

## Plane Deform selected surface patches 0.1.36

A native recessed-floor fixture with a chamfered corner reproduced a stationary
selected patch: its triangular corner cap added a third independent support
normal, producing a zero slide guide at one vertex. The common amount solver
then rejected every nonzero drag. Triangles now retain their natural ability to
rotate, while adjacent quad/N-gon support planes still supply the common tangent.
If a corner has no usable common tangent, it keeps its actual source edge rather
than passing a zero guide into the rest of the selection.

Projected corner guides stop at the fixed neighboring section even when no
external edge follows their exact direction. A common clamp also prevents
selected edges from collapsing/reversing on converging rails. Native writes
convert API centimeters to the scene unit, so the selected patch and helper
remain consistent in mm/m as well as cm. Unselected vertices, topology, UVs,
smoothing flags and component selection are preserved.

`.maya-tests/check_plane_deform.py` adds 13 native/offscreen checks: all eight
grips in both directions; triangular and four-sided corner caps; preserved
quad support planes; triangulated floors and vertex selection; repeated tilts
and retained helpers; fixed-row/collapse stops; transformed/scaled parents in
three scene units; preview/Cancel/Undo/Redo; the actual press/drag/release handler;
interior vertices; local/global/world frames and explicit axes; and native edge
ring sliding in mm. Logs use _0136 for Maya 2025 and Maya 2027. The full Draft
integration suite is also run with isolated application/temp directories.

## Edge Flow transverse strips and contour-only Relax 0.1.35

The user's selected wall cross edges were separate one-edge components.
0.1.34's Space treated each as an already spaced two-point chain, while native
vertex averaging included neighbors across the wall height and bent its planar
top/bottom surfaces. Edge Flow now identifies adjacent opposite selected edges
of a quad strip and follows its two longitudinal contours. Triangle pairs can
provide the same virtual quad boundary without treating their diagonal as a
cross edge. Open runs, closed rings and partial selections keep their boundary
anchors; ambiguous branching strips are rejected before editing.

Relax uses a half-strength neighbor average along each contour, with no
cross-wall contribution. An originally planar contour therefore stays in that
same plane. Space uses equal world arc-length steps along the two contours.
Both also retain support for ordinary selected chains. Native per-vertex writes
preserve topology/UVs/materials/smoothing, selection and one Undo step per click;
Relax no longer introduces a polyAverageVertex history node.

The suite now contains 19 native/offscreen checks. Seven new strip cases cover
the illustrated wall with planar flanges, repeated Relax without height drift,
equal spacing on both boundaries, partial runs and unselected sections, closed
strips with a fixed anchor, triangulation, transformed/scaled parents in mm,
mixed regular-chain/strip targets and Undo/Redo. Existing checks continue to
cover UI actions, ordinary paths, scene units, history/save/reopen, shading,
selection, invalid inputs and atomic rollback. Logs use _0135; fixtures run in
isolated Maya application/temp folders, without modifying the user's scene.

## Edge Flow panel 0.1.34

One standard-width Edge Flow button fills the free cell beside Fast Draft.
It toggles a themed bottom panel containing Relax and Space, with no extra
sliders. Opening/closing the panel does not edit geometry. The panel stays
available across selection and native manipulator changes; switching Model Pack
tools closes it and clears its checked button immediately. Reload and package
validation include the new module and both normal/checked SVG icon variants.

Relax runs one native polyAverageVertex iteration on the selected edge vertices.
Each click, including restoring edge selection on native Redo, is one Undo step.
Space redistributes vertices by equal arc-length steps along each original
world-space polyline, preserving open ends and one deterministic closed-loop
anchor. It keeps topology, UVs and neighboring unselected points. Distances are
converted from internal units for native vertex writes. Separate chains and
meshes are independent; branching Space selections are rejected before editing
any target. A native write failure rolls back the whole click. No new registered
plugin command, live geometry session or external dependency is introduced.

Twelve native/offscreen checks cover the real toolbox buttons/panel/toggle,
switching tools and changing selection, repeated Relax and Undo/Redo selection,
closed-mesh hard edges, equal straight spacing, bent paths and closed loops,
disconnected chains/multiple meshes, transformed parents and cm/mm/m units,
unchanged neighbors/topology/UVs/materials, history/save/reopen/Delete History,
invalid selections, branch rejection, no-op spacing and native failure rollback.
Tests use isolated Maya application/temp directories; logs use _0134.

## Keep Bevel end borders hard 0.1.33

0.1.31 softened every edge incident to a generated Bevel face, including the
transverse profile against its end surfaces. This spread the rounded normals
into the cube's end faces. Smoothing now includes internal generated edges and
only those perimeter edges whose endpoint anchors follow a selected source
edge. Perimeter edges within one source cross section keep their native flags
and authored end-face normals. Longitudinal rims blend into the supporting
surfaces; transverse end borders retain their sharp shading. The classification
uses source topology rather than world axes, current width or profile slope.

The native smoothing suite now contains seven checks. An independent direction
check distinguishes the longitudinal rims from transverse ends. A new case
verifies both planar end faces after positive/negative profile, width and local
width changes, plus commit. Existing checks cover locked normals, transformed
meshes, junctions, disconnected edges, transverse cuts, segment-count changes,
saved-node edits/reload, Undo/Redo, saved scenes and Delete History. Log suffix:
_0133. Source meshes are tested only in isolated Maya application/temp folders.

## Finalize zero-width existing Bevels 0.1.32

Recovered editing now records an exact requested Width 0, retaining the previous
tiny, valid strip in its disposable preview. Width can be raised again, locally
undone/redone, or cancelled without changing the source topology. Only Apply or
tool finalization welds the recognized section vertices to their recovered
corners, removes collapsed faces and hardens the surviving sharp seam. Welding
uses explicit section membership, so coincident vertices on another shell are
not merged. A positive width keeps its topology. The zero-width branch for a
saved Model Pack history restores its original unsplit base, including when
transverse cuts were added, then hardens only the original selected edges.

UV corner indices, unrelated smoothing flags and authored normal locks are
transferred to surviving faces. Material object groups follow face renumbering;
native groupParts keep multiple-material assignments serializable. Geometry
snapshots and cached Undo inputs retain object groups as well. The embedded
runtime capability gate refreshes an older loaded snapshot implementation at a
safe launch, without unloading the registered command or clearing committed Undo.
Finalization discards stale source components after the topology change and
preserves another object's current selection. Esc remains cancellation.

Twelve new native/offscreen tests pass in Maya 2025 and Maya 2027, covering
1/2/5 segments, unchanged live topology, re-expansion and local Undo, Cancel,
small positive widths, numeric zero, tool change and close, consecutive bands,
an unselected coincident shell, two UV sets/material assignments/locked normals,
Apply/Undo/Redo/save/reopen/Delete History, saved history with transverse cuts,
new-bevel cancellation at zero, failed-weld rollback, runtime upgrading with committed Undo, and the
transformed Detail04 bent strip and closed thin rim. Existing Bevel editing
(22) and history (9) checks pass on both versions. Maya 2025 also passes six rim
smoothing, nine curve-alignment and 13 face-control checks. Integration and
focus regression results are recorded in the _0132 logs. Tests use isolated
application/temp directories and never edit the user's working scene.

## Soft borders on multi-segment Bevels 0.1.31

The custom output copied the flat native template's hard rim flags even when
deformed into a rounded profile. When Segments is greater than one, generated
strip/corner faces are now identified by their collapse to the selected source
edges at zero width. Only edges incident to those faces become soft, including
their borders against the original supports. The adjacency/corner mask is cached
per template topology; changing width or profile reuses it. Other edge flags
and corner-normal locks remain as supplied by the native template. Locked
normals on the softened edges are released so imported normals cannot retain
a visible hard seam. Single-segment chamfers keep their previous shading.

Six new native checks cover 2/3/6 segments, live and Channel Box 1-to-many-to-1
transitions, unchanged source meshes on preview/Cancel, unrelated hard edges,
normal continuity across the rims, imported locked normals outside the strip,
corner junctions, disconnected selected edges, transverse cuts, transformed
meshes, matching native topology/UVs, Apply/Undo/Redo, saved scene reopen,
Delete History, saved-node editing and in-place plugin refresh.

Maya 2025 and Maya 2027 each pass all six smoothing checks, nine history checks,
22 existing Bevel editing checks and 113 integration checks (115 discovered,
two standalone UI/plugin checks excluded), using isolated application/temp
directories. Logs use _0131. The user's working scene is not used for tests.

## Three-decimal Bevel fields 0.1.30

The Width field used six decimal places and overflowed the compact numeric
control. Width now uses the same three-decimal presentation as Shape, for both
creation and recovered editing. Segment and transverse-cut counts stay integers.
Signal-blocked UI synchronization rounds the displayed value while leaving the
session's full-precision geometry parameters unchanged. Numeric edits commit
at the field's displayed precision; arrow/wheel steps retain their existing size.

The native Qt smoke check displays 1.889 / 0.960 for 1.888888 / .959999,
leaving the source state unchanged; integer counts retain no decimal suffix.
Existing typed-input fixtures use three-place input. Maya 2025 and Maya 2027
each pass all 22 Bevel editing checks and 113 integration checks (115 discovered,
two standalone UI/plugin checks excluded). Logs use _0130.

## Preserve live ReCurve sessions across application focus 0.1.29

The application-deactivation handler queued Cancel and hid the settings panel,
discarding both live ReSegments previews and Control Curve cages on Alt+Tab.
Native geometry plus Qt application events reproduced six session-loss cases
before the fix. Deactivation/activation now synchronously clears transient
Alt-tap and Space-apply state without queuing any mesh/session/panel change.
Input releases received while inactive cannot commit a panel-less Shift+Wheel
preview or toggle its topology mode. Internal window/editor focus changes retain
their previous behavior. Pending numeric and wheel edits stay in the idle queue.

The embedded runtime capability check requires the new listener method, so an
already loaded older module from the same canonical folder is refreshed at the
next safe toolbox launch. The update bridge's active-tool wait remains intact.
Ordinary Apply, Cancel, scene changes, toolbox close and tool switching retain
their existing session lifecycle.

Nine new native/offscreen checks cover repeated application switches with
changed segment counts/topology mode/live Undo, aligned and edited CVs, unchanged
guide identities/selection/timers/Undo stack, face-contour controls, continuing
CV edits and Apply/Undo/Redo, Cancel restoring the original mesh input, pending
wheel/count edits, a typed field on focus loss, fractional scrolling, Alt release
after Alt+Tab, a fresh Alt tap, inactive Shift release without a panel, internal
window focus and the runtime upgrade gate. Qt events are delivered through the
installed application filter; another desktop application is not controlled.

Maya 2025 and Maya 2027 each pass all nine focus checks, nine existing curve
alignment checks, 13 existing face-control checks and 113 continuing integration
checks (115 discovered, two standalone UI/plugin checks excluded). Tests use
isolated Maya application/temp directories. Logs use _0129.

## Bent single-segment Detail04 chamfer 0.1.28

The new user FBX is retained separately as Detail04_LongStrip.fbx (112 vertices,
88 polygons; SHA256 a450545acfc4fdd33004cd14e3501d36241ccd03b84e07948f0956fd63e5748a).
The illustrated 13-face longitudinal chamfer failed direct strip reconstruction
at section vertices 17/43: outgoing directions on its warped supports were skew
in 3D. The planar-support solver also rejected those incompatible face planes.

Existing successful section reconstruction is unchanged. Only a failed section
with two endpoints uses the surrounding section midpoints to estimate the strip
tangent and its transverse plane. Projecting the support directions into that
plane produces a local sharp corner without shifting the section endpoints.
Distance, direction and surviving-face orientation checks still apply. The
analysis cache includes the inferred plane, keeping rejected unprojected and
accepted projected sections separate. Classification and edge-selection rules
are unchanged: this fixes explicit polygon-band editing/removal.

Eight native/offscreen checks on that fixture cover read-only and cached
recognition, an unchanged initial preview, numeric and mouse width edits,
segments/profile editing, source vertices outside the band, support orientation,
Apply/Undo/Redo and save/reopen with UVs, the actual toolbox Bevel and UnBevel
actions, rotated/scaled parents, cm/mm units and rejection of main surfaces.
UnBevel collapses 14 sections, removing 14 vertices and all 13 selected polygons
(112/88 -> 98/75), without non-manifold geometry or degenerate remaining faces.
The tests import a separate copy in isolated Maya application/temp directories;
the user's live scene is not edited. Hardware viewport picking is not claimed.

Maya 2025 and Maya 2027 each pass all eight new long-strip checks, seven
existing Detail04 rim checks, 22 existing Bevel editing checks and 113 continuing
integration checks (115 discovered, two standalone UI/plugin checks excluded).
Logs use _0128.

## Bevel history deletion 0.1.27

Native Delete History baked the rendered mesh but left the Model Pack node and
its native topology templates detached in the scene. The saved node's segments
output drove the same templates feeding its mesh inputs, creating a node cycle.
The segment value now belongs to one upstream native network parameter. Both
templates and the saved node read it; a per-node dynamic proxy exposes the same
Segments label in the Channel Box. The registered input remains compatible with
older plugin classes and scene files. No Maya commands, hotkeys or global menu
procedures are replaced.

Toolbox reload upgrades earlier connected histories in place, preserving an
authored segment driver and the current geometry. The initial toolbox import
also performs that upgrade. This does not delete previously detached scene
nodes or change the user's working geometry during reload. Preview teardown and
saved-template replacement include their temporary parameter sources.

Nine native checks cover Delete History (with/without transverse cuts), Maya's
object menu command, Non-Deformer History without deformers, exact evaluated
points/topology/UVs/normals/materials, unrelated objects, native Undo/Redo,
two committed bevels followed by polySoftEdge, shared output branches, segment
and width editing, an empty saved edge selection, recovered baked-band edits,
old-layout migration with animated segments, and old/new .ma/.mb round trips.
All geometry tests run in isolated Maya application/temp directories, without
editing the user's live scene. Hardware mouse interaction is not claimed.

Maya 2025 and Maya 2027 each pass all nine new history checks, all 22 existing
Bevel editing checks and 113 continuing integration checks (115 discovered,
two standalone UI/plugin checks excluded). Logs use _0127.

## Surface polygon centers and HiDPI hover 0.1.26

A warped quad's average vertex position can lie between its triangles. The old
surface-distance check then rejected its polygon-center snap. The center is now
projected along the polygon normal onto that polygon's native triangulation,
testing triangle coverage before accepting it. Planar face centers and edge
midpoints keep their positions. Concave outlines/holes do not borrow a point
from another polygon or snap to an arbitrary nearest boundary. Visibility and
same-face ray checks remain in place.

Hover translates Qt logical cursor coordinates into native Maya viewport pixels,
including the bottom-origin Y conversion, before ray picking or snap lookup.
The first-click gesture continues using native dragger coordinates. This makes
the hover marker agree with the drawing origin at scaled viewport sizes.

Four added checks cover a previously failing warped quad with both shapes,
transformed/instanced warped faces, 12 visible nonplanar polygons from the newer
Detail04 fixture, and native perspective rays through the host hover/first-click
path at 100/125/150/200% viewport-to-widget scales. Existing concavity, occlusion,
per-face subdivision, center checkbox, edge midpoint and momentary V/C/X checks
continue. Native geometry/camera/Qt tests do not claim hardware mouse verification.

Maya 2025 and Maya 2027 each pass 113 checks (115 discovered, two standalone
UI/plugin checks excluded). The focused snap suite passes all 22 checks. Logs
use _0126. Tests use isolated Maya application/temp directories and never modify
the user's live scene.

## Narrow Detail04 rim recognition 0.1.25

The newer user-provided Detail04 FBX has 431 vertices and 368 polygons. Its front
collar has two narrow rows bordering a broader curved fillet. The prior seed
classifier missed it because its final facets are nearly tangent to a support,
and unrestricted profile continuation entered the neighboring hole transition.
Explicit complete bands already reconstructed correctly.

Unresolved quad seeds now have a bounded fallback requiring a thin profile, a
clear width break on both sides, a consistent normal transition and a complete
reconstructible band. Continuation retains whole profile cells. Terminal cells
are trimmed only when the larger band fails full reconstruction, preserving the
seed and selecting the largest valid extent. The classifier remains read-only;
flat grids, main faces and uniform cylindrical walls retain their rejection.
Existing recognized profiles and explicit-band priority keep their original path.

Seven native/offscreen regressions on a separate copy of that FBX cover picks
from either row and mirrored side, exactly the same 32-face extent, unchanged
source topology/UVs, mouse and numeric Bevel editing, segment changes, no flipped
neighbors, Apply/Undo/Redo, selected search through the actual toolbox and full
UnBevel removal (34 vertices and 32 polygons removed). The earlier Detail04
fixture is retained separately. Hardware viewport picking is not claimed.
Maya 2025 and Maya 2027 each pass all seven new checks, all 22 existing baked
Bevel checks and the 109 continuing draft/integration checks (111 discovered,
two standalone UI/plugin checks excluded). Logs use _0125. Tests run in isolated
Maya application/temp directories and never import into the user's live scene.

## Shared multi-mesh mirror plane 0.1.24

Mirror Cut and Mirror Dup initialize multi-object selections with one world-space
plane at the combined bounds center. World uses the world basis; Local uses the
last selected object's rotation without scale/shear. The two initial frames are
captured before creating mirror output, so changing World/Local cannot derive
different centers from already reflected meshes. Single-object pivot behavior
is retained. Bounding-box values are converted from scene units to API centimeters.

The viewport draws one plane/manipulator sized for all members. Live movement,
rotation, numeric angle entry and snap-cache restoration update all member
frames. A single Undo chunk commits each plane edit; cancellation restores all
snapshots. Axis/direction/threshold changes and space changes keep one common
frame. A completely discarded cut member skips Maya's invalid empty-mesh shader
query while retaining normal Apply/Undo/Redo behavior.

Eleven native/offscreen regressions cover both mirror operations, multiple
rotated/scaled/sheared transforms and parents, combined centers, last-selected
orientation, scene units, one aggregate gizmo, live drag/cancel, numeric rotation,
snap-cache restoration, axis/space/direction changes, Apply and native Undo/Redo,
including an entirely discarded mesh. Maya 2025 and Maya 2027 each pass all 11
and the 109 continuing draft/integration checks (111 discovered, two standalone
UI/plugin checks excluded). Logs use _0124. Viewport event routing is modeled;
these results do not claim hardware mouse input verification in the user's scene.

## Optional edge flow alignment 0.1.23

The embedded Control Curve panel has an English **Align to Curve** checkbox,
unchecked for every new preview and shown only for edge controls. Enabled mode
evaluates the current guide directly at each selected chain's normalized chord
parameters, rather than adding only the guide's delta to the original profile.
It updates immediately and follows subsequent CV/transform edits. Repeated
updates do not accumulate drift. Open endpoints remain constrained by the fitted
curve endpoints; periodic loops and independent guides keep their own bindings.

The underlying unaligned profile is retained across control-point refits, so
disabling the checkbox restores original detail plus prior CV edits. Topology,
UVs, edge smoothness, unselected vertices and existing face-contour behavior
retain their existing rules. Apply/Cancel and Maya Undo/Redo use the shared
session. Checkbox actions use the existing guarded idle queue.

Nine dedicated native/offscreen checks cover immediate fitting, default-off
geometry, live CV edits, reversible refits, open endpoints, periodic/multiple
chains, transformed meshes, Apply/Undo/Redo and the embedded UI. A loaded older
canonical ReCurve runtime is detected and refreshed through its existing safe
installer; a regression exercises this same-process upgrade. The panel capture
was inspected for label placement and fixed width. This does not claim hardware
viewport input verification.

Maya 2025 and Maya 2027 each pass those nine checks, all 13 face-contour checks
and 109 continuing draft/integration checks (111 discovered, two standalone
UI/plugin checks excluded). Logs use _0123. No tests use the user's working scene.

## Momentary V/C/X snapping 0.1.22

Fast Draft now owns V/C/X press/release events and ShortcutOverride. Its drawing
engine reads only the held key, so stale native snapMode flags cannot re-enable
snapping after release. Releasing a key updates a still base synchronously and
preserves its chosen origin. Native tolerance, geometry resolution and grid
settings remain in use; global snap flags and key bindings are not modified.
Toolbar snap flags no longer activate Fast Draft snapping on their own.

Auto-repeat cannot toggle or duplicate held keys. Owned releases are handled
even outside the viewport or after modifier/focus changes. Unknown releases,
text input and unrelated shortcuts pass through. Deactivation clears keys
without cancelling geometry; Esc, shape restart and shutdown also clear them.
If multiple snap keys are held, the most recently pressed one takes precedence.

Regressions reproduce persistent Maya flags while checking each V/C/X release,
one vertex anchor followed by free sizing at multiple other vertices for both
cube and cylinder, immediate Ctrl-centered grid release, unchanged native flags,
auto-repeat, release outside the viewport, multiple keys and lifecycle cleanup.
These are native Maya geometry and offscreen Qt checks, not hardware input tests.
Maya 2025 and Maya 2027 each pass 109 continuing checks (111 discovered, two
standalone Fast Draft UI/plugin tests excluded). Logs use the suffix _0122.

## Immediate drawing cancellation and Q/W/E/R 0.1.21

Esc and plain Q/W/E/R claim ShortcutOverride in the drawing viewport/panel.
KeyPress cancels the unfinished footprint synchronously, before any late mouse
release can commit it. Esc retains the drawing tool. Q/W/E/R then stop Fast Draft
and activate Maya's existing Select/Move/Rotate/Scale contexts on the next event
turn. Pending wheel/key refreshes cannot resurrect a cancelled preview. A later
tool choice takes precedence over a queued switch.

ToolChanged is observed with a deferred context check, so toolbar switches clean
the session without interrupting an internal shape restart. Cancellation also
handles captured key events routed to the viewport's top-level window. Text
inputs, modified shortcuts and unrelated windows are excluded. D/Insert have no
added cancellation behavior. The four native context names were confirmed in
the running Maya 2025 session without switching its tool.

Six added offscreen/native-geometry regressions cover Esc before mouse release,
all Q/W/E/R × three drawing phases, unchanged source points and Undo head,
queued wheel cleanup, ShortcutOverride, text/modifier/input ownership, shape
restart callbacks, subsequent tool choices and top-level viewport key routing.
The standalone context switch is modeled; these tests do not claim hardware
mouse/key verification. Maya 2025 and 2027 each pass 103 continuing checks (105
discovered, two standalone Fast Draft UI/plugin tests excluded). Logs use _0121.

## Maya point, curve and grid snapping 0.1.20

Fast Draft reads native snapMode point/curve/grid flags, so Maya's V/C/X press
and release commands and toolbar modes drive both first-point placement and the
base drag. Hotkeys are forwarded unchanged. A short timer also observes delayed
Modeling Toolkit release state while dragging with a still pointer. Native modes
override automatic centers, independently of the centers checkbox. Depth stays
fixed to the existing signed drag workflow.

Cached visible mesh vertices are searched in screen space with Maya's tolerance
and ray occlusion checks; a silhouette vertex can start a drawing without an
initial surface-ray hit. Curves use native closestPoint and plane crossings,
rather than CV-only snapping. Points/curves are constrained to the base plane.
Grid snapping accounts for divisions and scene units on that plane. Original
mesh/isolate/selection-only visibility filters apply to snap targets.

Thirteen added tests exercise native geometry for points, silhouettes, occlusion,
curves, nonplanar curve crossings, grid units/divisions, rotated planes, base
placement, Ctrl proportions, release refresh, depth isolation and Qt forwarding.
Standalone has no snap manager (snapMode returns None), so only that UI service
is represented by a state shim; geometry, curves, rays, grid settings and Qt are
real. An idle native Maya 2025 probe verified the six installed V/C/X bindings
and each press enabling its matching snapMode flag, then restored flags. Context,
selection and Undo were unchanged. The recorded release state was queried before
deferred toolkit work; the timer and a delayed-state regression cover this case.
This does not claim hardware mouse/key gesture verification.

Maya 2025 and Maya 2027 each pass 97 continuing checks (99 discovered, two
standalone Fast Draft UI/plugin tests excluded). Logs use the suffix _0120.

## Polygon-center snapping 0.1.19

The object bounds candidate is replaced by the vertex center of the polygon
under the pointer, computed in world space. Edge midpoint candidates, the 12-pixel radius, marker color,
preferences and first-point-only behavior remain. A polygon center must also
ray-pick back to the same face; concave/holed centers lying on another polygon
of the same mesh cannot redirect the drawing anchor.

Three added native regressions check all four face centers of a subdivided cube,
the absence of the former object-center anchor, a cylinder drawing on an offset
face, an asymmetric triangle's center, and a concave face whose center lies on
another face of the same mesh. Existing transformed, instanced, occluded, input
and marker checks now expect polygon centers. Maya 2025 and 2027 each pass 84
continuing checks (86 discovered, two standalone UI/plugin tests excluded).
Logs use the suffix _0119. Native geometry and Qt checks run in isolation.

## Independent square and center modifiers 0.1.18

Shift constrains the Cube footprint to an equal-sided square from the first
corner. Ctrl alone centers a free-proportion rectangle around the first point;
Shift+Ctrl combines both into a centered square. The unchanged input filter
refreshes a still drag when either modifier is pressed or released. Hints and
tooltips now describe the separate shortcuts.

Existing native/offscreen regressions now cover Ctrl alone, both modifiers,
all quadrants and zero-axis drags, key transitions with a still pointer,
snapped centers, actual Add/Cut geometry, and a fixed base during depth editing.
Maya 2025 and 2027 each pass 81 continuing checks (83 discovered, two standalone
Fast Draft UI/plugin tests excluded). Logs use the suffix _0118.

## Cylinder orientation and Boolean cleanup 0.1.17

The drawing frame now projects the object's local axes onto the surface instead
of following an arbitrary longest polygon diagonal. Carve's planarity tolerance
accounts for the cutter's world coordinate precision; a rotated, nonuniformly
scaled beveled test object at world Y=390 reproduces fragmented cap faces with
the old tolerance and a single 8-gon floor with the corrected tolerance.

New near-coplanar edges are dissolved using native polyDelEdge history after
the Boolean. Geometric matching against source face boundaries protects original
edges and their trimmed portions. This also permits native N-gons with inner
hole loops on the outside surface. Source points and history remain untouched.
The reported cut was read as geometry data into an isolated fixture; cleanup
removes seven added radial edges without moving its points or losing UVs.

Seven new native checks cover irregular/triangulated face orientation, preview
and committed cylinder rims, perpendicular normals, forced triangulated Boolean
output restored to one floor N-gon, source grid edge preservation, the reported
mesh fixture, transformed beveled geometry far from the origin, repeated/blind/
through cuts, manifold checks, UV assignments, Undo/Redo and history scene reload.
Maya 2025 and 2027 each pass 81 continuing checks (83 discovered, two standalone
Fast Draft UI/plugin tests excluded), plus 22 Bevel and 13 face-control checks.
Logs use the suffix _0117 in ../.maya-tests/. Tests use isolated native geometry
and offscreen Qt; they do not claim hardware mouse gesture verification.

## Fast Draft center snapping 0.1.16

An optional, persisted center-snapping checkbox controls only the first drawing
point. Candidates are the mesh's object-space bounds center transformed to world
space and projected onto the picked surface plane, plus midpoints of that
polygon's boundary edges. A 12-pixel screen radius, on-surface validation and
nearest-visible-mesh ray tests prevent snapping into concavities or through
occluders. Hover uses a read-only throttled timer and an input-transparent
turquoise marker; base/depth gestures keep their fixed anchor. Offscreen tests
exercise viewport ownership, modifier gating, preferences and timer cleanup.

Fifteen added checks cover exact native geometry, all four box edges, zoom,
rotated/nonuniformly scaled meshes and moved pivots, instances, occlusion,
concave surfaces, perspective/orthographic Maya camera matrices, free placement,
snapped Shift+Ctrl squares, cylinder wheel/creation, marker pixels and lifecycle.
A read-only native Maya 2025 viewport probe also exercised real M3dView camera
projection, ray picking and midpoint snapping, with selection and Undo unchanged.
The native probe did not simulate hardware mouse gestures.

Maya 2025 and Maya 2027 each pass 74 continuing checks (76 discovered, two
standalone Fast Draft UI/plugin tests excluded). The unchanged Bevel and
face-control suites add 22 and 13 checks respectively. Logs use the suffix _0116
in ../.maya-tests/.

## Polygon-only existing-Bevel editing 0.1.15

Explicit polygon components are now required for existing-bevel recovery and
saved-history editing. All edge selections, including transverse profile edges
and borders adjacent to existing bevels, retain new-Bevel behavior on the exact
selected edges. Object and mixed face/edge selections cannot trigger recovery.
The edit menu uses the same rule. Saved Model Pack history is reopened only when
the selected polygons belong to the currently connected bevel output.

Maya 2025 and Maya 2027 each pass 22 bevel checks, 13 face-control checks and 59
continuing checks (96 discovered, two standalone Fast Draft UI/plugin tests
excluded). Native checks include new geometry on an existing bevel's selected
edge, exact source-edge IDs, unchanged source points/topology on Cancel, saved
history remaining unchanged while creating another bevel, object/mixed selection
gating, menu gating, rejecting parent faces for saved-history editing, polygon
editing after Apply/Undo/Redo, and all existing drag/activation regressions.
Logs use the suffix _0115 in ../.maya-tests/.

## Native Bevel activation and end grips 0.1.14

A transient native Maya 2025 probe on an owned copy reproduced the missing mouse
editing session: the real toolbox button changed from face selection to the edge
mask, active selection became empty and _SESSION stayed None. The source user's
geometry was not edited and the undo head was unchanged. In standalone this
filtering did not occur, which is why earlier simulated-context tests passed.

Activation now resolves the selected band before switching contexts/masks and
hilites its owned preview. Recovered end-profile picking uses visible profile
segments before requiring a surface ray; a grip just outside the silhouette can
therefore start a local-width gesture.

The baked-bevel suite has 20 checks, including an interactive-mask-filter
regression and all four press/drag/release modes on recovered end profiles:
width, segments, profile and local width. The latter also checks unequal local
endpoint weights and unchanged source points. Standalone drag coordinates are
simulated; native context verification is recorded separately.

Maya 2025 and Maya 2027 each pass 20 bevel checks, 13 face-control checks and 59
continuing checks (94 discovered, two standalone Fast Draft UI/plugin tests
excluded). Logs use the suffix _0114 in ../.maya-tests/.

## Bevel numeric input 0.1.13

An end-to-end offscreen toolbox regression reproduces a UI failure missed by
calling ui._numeric_value directly: Return in the real spin box propagated to
the toolbox QDialog, activated its default Combine command and ended Bevel.
Arrow changes also waited for editingFinished. Numeric fields now consume Return
after normal value confirmation, and committed valueChanged signals update the
preview immediately. Decimal entry uses Maya-style dot notation and also accepts
comma, independently of the system locale.

The 18-case baked-bevel suite includes the actual ModelPackToolbox button and
Qt keyboard entry on a geometry snapshot of the user's 12-vertex, two-segment
cube. It checks Return without another toolbox command, immediate width steps,
segment steps, comma input, a retained session, changed preview points, unchanged
source geometry and viewport drag callbacks. The existing 17 geometry/FBX cases
continue to pass. Native context switching and drag coordinates are simulated
in standalone; the complete toolbox and numeric widgets are real. A separate
interactive Maya launch did not finish startup, so these results do not claim
hardware viewport/input verification.

Maya 2025 and Maya 2027 each pass 18 bevel checks, 13 face-control checks and 59
continuing checks (92 discovered, two standalone Fast Draft UI/plugin tests
excluded). Logs use the suffix _0113 in ../.maya-tests/.

## Explicit transverse Bevel selection 0.1.12

The supplied Detail04.fbx reproduces the rejected transverse selection: automatic
seed search prefers the intersecting longitudinal fillets and rejects transverse
support faces. Complete explicit polygon bands are now validated directly before
seed expansion. Single-face and edge seed expansion retain their previous path.

The native baked-bevel suite now contains 17 checks. Three regressions import the
user's FBX from ../.maya-tests/fixtures/Detail04.fbx and exercise the two-row
shoulder band, Bevel button activation, width decrease/increase, unchanged outside
vertices, positive support-face areas, Cancel, two-to-four segment rebuilding,
Apply/Undo/Redo, UV assignments, smoothing flags and scene save/reopen. Saved UV
coordinates are compared within 1e-6 to allow Maya ASCII decimal serialization;
in-memory assignments and coordinates remain exact.

Maya 2025 and Maya 2027 each pass all 17 baked-bevel checks, 13 face-control checks
and 59 continuing regressions (91 discovered, two standalone UI/plugin checks
excluded). Logs use the suffix _0112 in ../.maya-tests/. Checks use isolated Maya
preferences and temporary folders, with native geometry and offscreen Qt. Tool
context switching is simulated in the offscreen button test; these checks do not
claim a manual viewport gesture review. The user's open scene is not changed by
the tests.

## Editing baked Bevel bands 0.1.11

Maya 2025 and Maya 2027 each pass all 14 baked-bevel checks, 13 face-control
checks and 59 continuing regressions (88 discovered, two standalone UI/plugin
checks excluded). Source syntax and final manifest hashes are verified.

Native checks in ../.maya-tests/check_bevel_edit.py cover seeded face and edge
recognition after history deletion; no opening drift; Cancel restoring geometry,
visibility, selection and the undo head; physical width matching native bevels;
segment changes and flat/round/concave profiles; closed cylinder bands; local
width with session Undo/Redo; support-face width limits; one native Apply Undo;
reopening edited geometry; transforms; UV preservation including named sets,
retopology and save/reopen; rollback after an injected commit failure; the actual
Bevel activation/numeric-controls workflow; rejecting ordinary faces unchanged;
and existing new-Bevel/saved-history editing.

Logs: ../.maya-tests/check_bevel_2025_0111.log and check_bevel_2027_0111.log.
These are native Maya geometry/DG and offscreen Qt checks. Recovered parameters
are geometric estimates; general corner networks and arbitrary sculpted shapes
are not claimed as recoverable strips. Full Fast Draft/update and face-control
regressions are also run for this release.

## Face-region Control Curve 0.1.10

Maya 2025 and Maya 2027 each pass all 13 face-control checks and the existing
59 regression checks (74 discovered in total, with two standalone UI/plugin
checks excluded). Python source and final manifest hashes are validated.

The dedicated native Maya suite ../.maya-tests/check_face_controls.py covers 13
cases: no initial fitting drift; smooth interior and unchanged outside vertices;
whole-guide and single-CV edits; UV sets, hard/soft edges and authored normals;
Apply/Cancel and native Undo/Redo; refitting 3–32 CVs without losing shape;
disconnected islands, shared corners and holes; cylindrical side bands;
triangles and single quads; transformed meshes at large world coordinates;
save/reopen; a region with over 900 interior vertices; rejection of a closed
selection before any edit; existing edge controls; and the embedded Model Pack
button/count-field workflow. Full Fast Draft/update regression checks continue
in ../.maya-tests/check_draft.py.

Logs: ../.maya-tests/check_faces_2025_0110.log, check_faces_2027_0110.log,
check_draft_2025_0110.log and check_draft_2027_0110.log. These are real Maya
geometry, DG, NURBS and offscreen Qt input checks, not a manual viewport review.

## Square drawing modifiers 0.1.9

Maya 2025 and Maya 2027 each pass 59 checks (61 discovered, two standalone
UI/plugin checks excluded).

Native Maya geometry and offscreen input checks cover Shift squares anchored at
the clicked corner and Shift+Ctrl squares centered on the original click. All
four drag quadrants, axis-aligned drags and tilted/reversed surface planes keep
the click fixed. Live modifier changes update a still mouse gesture; modifiers
after the base release leave the footprint unchanged during the depth gesture.
Added rotated meshes match their preview vertices, centered cuts have the
expected floor, volume and copy dimensions, and Undo/Redo work. Cylinder radius,
section controls, six-face cube topology and toolbar-color rendering are covered
by the continuing regression suite. Latest logs: ../.maya-tests/check_draft_2025_019.log
and ../.maya-tests/check_draft_2027_019.log.

## Fast Draft viewport color 0.1.8 — 2026-10-19

Maya 2025 and Maya 2027 each pass 54 checks (56 discovered, two standalone UI/plugin checks
excluded). The color check now renders the actual QPainter wire to an image and
checks its pixels against the toolbar's #5285A6, for Cylinder / Cube at base,
positive and negative depths, even with a gray application highlight palette.
Another check covers one reused, input-transparent canvas during section-count
changes and timer teardown.

The user's running Maya 2025 viewport also exercised all six shape/depth cases
with a separate transient preview. Its native overlay was visible and its
post-render callback bound to modelPanel4. Each case produced 1,816–6,900 opaque
pixels of exactly #5285A6. Projection agreed with Maya's native worldToView;
selection, undo head, active context, drawing state and scene-modified state
were preserved. The temporary mesh, canvas, callback and timer were removed.
Artifacts: ../.maya-tests/native_draft_color_018.json and native_draft_wire_018.png.
The previous native reference-wire approach could still draw black; the new
overlay paints UI sRGB independently of the VP2 wire color and ACES transform.

## Local automatic updates 0.1.7

Maya 2025 checks include manifest validation, rejecting incomplete releases before
closing an open window, deferring while Reset Tube is active, retaining a closed
toolbox, canonical package search paths, and an actual tool-window reload which
preserves a created cylinder's points, cap setting and selection. The existing
geometry and cap checks remain part of the suite.

The bridge was connected to the user's running Maya 2025 through its existing
local MEL command port. The stable module and MP shelf command were installed
with preference backups. The bridge reports a queued update while the user's
Reset Tube tool remains active. No scene save or geometry finalization was used
to force an update.

## Cylinder smoothing and editable caps 0.1.6 — 2026-10-19

Native Maya 2025 and Maya 2027 each pass 48 checks (50 discovered, with the two
standalone UI/plugin checks excluded). New checks cover soft cylinder wall edges
and hard cap rims; Cap / Center / Parallel for odd and even side counts (3–32);
vertex counts, manifold topology, volume and assigned UVs; proxy Channel Box
parameters; history with the global history toggle disabled; Undo/Redo; unit
changes and rotated frames; blind and boundary-crossing cuts without floor
center vertices; smooth cut walls with retained or baked Boolean history; and
the T menu, unrelated objects, modifier keys, text input and pending menu teardown.

A saved scene was additionally opened in an independent Maya 2027 process with
the package's plugin search path, without preloading Model Pack. Maya loaded the
history plugin from the scene requirements and all three cap modes evaluated.
An additional Maya 2027 process loaded the supplied 0.1.5 modifier plugin first;
the new cap node was then registered into that already loaded plugin and evaluated
successfully, using plugin names rather than the new package's filesystem path.
These remain native geometry/offscreen Qt checks, not a manual desktop viewport
validation. The existing 0.1.5 behavior checks are retained below.

## Fast Draft fixes 0.1.5 — 2026-10-19

Maya 2025 and Maya 2027 each pass 39 checks (41 discovered, with the same two
standalone UI/plugin checks excluded). Python syntax and the final archive's
installer manifest are verified as part of release packaging.

The updated suite runs native Maya geometry and offscreen Model Pack Qt checks.
It tests the explicit turquoise RGB override with a gray application palette,
Reference display (Template forces gray), six-face cubes despite old subdivision
preferences, no cube Shift-wheel changes or square constraint, and standard
single-column Fast Draft button width.

Depth-gesture regressions cover positive/negative X/Y/Z normals, diagonal faces,
rolled camera axes, two different second-click locations, perpendicular mouse
motion, outward/inward sign changes, a frozen axis during the gesture and the
explicit upward/downward fallback for face-on or nearly face-on normals. These
screen projections are simulated; preview/primitive meshes and booleans are real.
The manual running Maya viewport has not been exercised in this validation.

The remaining behavior checks from 0.1.4 are retained, with cube subdivision and
palette-dependent color assertions updated to the new requested behavior.

## Fast Draft integration 0.1.4 — 2026-10-19

That integration was tested with native Maya 2025 and Maya 2027 in separate
mayapy processes. In each version, 36 checks passed; two standalone Fast Draft
UI/plugin tests were intentionally skipped because this package uses the embedded
Model Pack host. Python sources and release file hashes are checked separately.

Checks cover native geometry creation, subtraction, rotated/scaled surfaces,
units, original-target preservation, Undo/Redo and rollback after failed edits.
They also cover both drawing gestures, transient preview/undo isolation, native
palette colors at positive/negative depth, live cylinder topology, subdivided
cube previews/creation/cuts, wheel limits and partial detents, unrelated widgets,
viewport widget ancestry, shape changes, Esc, external context changes, tool
switches, queued input, scene changes and rebuilding the toolbox with a live
preview. The actual Model Pack Qt window was rendered and inspected with both
shape choices; its panel has no sliders or editable count fields and retains the
compact window width.

These are offscreen Qt tests with real Maya geometry. Maya standalone has no
interactive tool context or viewport; context selection and input ray/view
sources were simulated where necessary. The tests do not establish physical
mouse delivery, camera interaction or the final highlight appearance inside a
running Maya desktop viewport. Existing Model Pack/ReCurve algorithms were not
changed. The notes below describe the supplied baseline validation, not a new
complete regression pass for every original feature.

## Supplied ReCurve 0.1.3 baseline

Build: **Model Pack + ReCurve 0.1.3-recurve**.

The integration was exercised in separate Windows processes with Maya 2025 and
Maya 2027, using native Maya geometry and the actual Model Pack Qt window.
Qt ran offscreen and mouse/key input was dispatched to test widgets. These checks
do not replace a complete manual session in the user's Maya viewport.

Checked workflows:

- Both new buttons occupy the final grid row. Settings are children of Model
  Pack, with no extra floating ReCurve dialog while the toolbox is open.
- The expanded count fields, topology choice, Apply and Cancel fit within the
  original window width. Native screenshots were inspected for readability.
  Section changes preserve that width. Only the active tool's wheel hint is
  visible; both hints disappear when the settings close. Redundant shortcut
  text between the controls and under Apply/Cancel is removed.
- Transformed cylinders with upper and lower rings selected; live segment
  changes; retained preview when switching sections; automatic curve creation.
- Ctrl+Shift+Wheel refits controls, curve CV moves update the mesh, and
  Shift+Wheel works only in active ReSegments and does not start a background
  preview. It is ignored while Control Curve is selected.
- Alt switches topology. Apply/Undo/Redo and Esc/Cancel restore the expected
  geometry. Space applies; releasing Shift leaves the embedded preview active.
- The exact Shift+R runtime-command body opens the embedded settings. Hotkey
  registration was verified while preference writes were intercepted in tests.
- Closing the toolbox or switching tools cancels its unfinished preview and
  removes guide curves. Pending wheel/count work is discarded.
- Another active Model Pack tool retains its input. Reopening/reloading Model
  Pack with a live curve cage cancels that cage before rebuilding the window.
- Closing Model Pack restores the standalone ReCurve shortcut fallback.
- Maya 2025 additionally checks updating a separately loaded ReCurve 0.8.5 with
  an active deformed cage, restoring its original mesh before loading this copy.
  Replacing loaded Model Pack 0.1.1 files in the same folder also reloads the
  bundled runtime to the current build and enables live segment Undo.
- Live count Undo with Ctrl+Z and redo with Ctrl+Shift+Z/Ctrl+Y preserve the
  expected mesh, including when a deformed guide cage remains active.
- Native evaluated mesh snapshots work with no history, deleted history, history
  recording off, triangulation, smoothing, bevel/extrude history, bend, lattice,
  blend shape, skinCluster, a plain mesh input, and animated vertex tweaks.
  Cancel and native Apply/Undo/Redo restore the original input connections.

The six ReCurve helper modules match the tested standalone 0.8.5 files byte for
byte. The bundled main module adds panel-host/input hooks, count-Undo notifications,
and history snapshot handling; resampling algorithms remain unchanged. All package Python
sources were parsed, and the release checksums and ZIP contents were verified.

The supplied original Model Pack release identifies Maya 2027 as its upstream
tested environment. This integration pass does not revalidate every original
Bevel, Mirror, Selection or Plane Deform feature on other Maya versions.

## Fast Draft Line Sketch 0.1.72

Native click-polyline sketch on the first picked surface plane. Angular step, parallel/perpendicular inference, selected coplanar edge directions, click closure, Enter completion, editable native curves, open extrusion and closed Boolean subtraction. Isolated native/offscreen checks: check_line_sketch.py (8 checks) and check_draft.py (115 checks), Maya 2025 and 2027. Existing cylinder/box and momentary V/C/X modes preserved.

## Fast Draft cleanup 0.1.73

Clear Sketches removes only Fast Draft-owned native curves (including earlier saved patterns and tagged legacy Line Sketch). User-added mesh children are preserved. Clean History bakes owned output meshes and legacy Fast Draft cylinder cap histories, removes their proxy controls and empty operand groups. Original hidden source meshes and unrelated geometry/history remain intact. Native geometry and Undo/Redo cleanup regressions plus standard Draft and Line Sketch suites run in isolated Maya environments.

## ReSegment material binding and Offset regions 0.1.74

Reproduced loss of a uniform material assigned via mesh faces after resampling. MeshSource now holds a reversible native whole-shape shading link, temporarily disconnecting obsolete face membership links without deleting upstream groupParts/history. Cancel/Undo restore the exact original connections. Applied links serialize through native Maya connections. Native checks cover preview/resample/apply/Undo/Redo, upstream history, vertex colors, MA/MB reopen and Delete History. Existing one-material restriction remains.

Offset accepts a connected polygon region on one mesh. A single exterior boundary is extracted; shared interior edges are dissolved on the disposable preview and in the native Apply chunk. Result is one inset cap with a shared radius-preserving outer contour. Cancel leaves original internal topology untouched; Undo restores it. Disconnected regions and regions with holes are rejected before scene changes. Selected faces must share one material.

## Dependent geometry protection during Clear Sketches 0.1.75

A deleted curve can leave a saved Floater surface feeding empty mesh data into a downstream native Bevel; the supplied scene reproduces a Maya 2025 crash during file open. Clear Sketches now discovers dependent renderable meshes and bakes their current evaluated history in the same Undo chunk before deleting source curves. Invalid dependent geometry is rejected before any deletion. Regression includes Floater + native Bevel, geometry equality and Undo/Redo. Scene repair is a separate copy with the unrecoverable empty floater chain and 401 orphan node blocks removed; 224 remaining mesh geometries, UVs, matrices and material assignments compared against the recoverable source.

## Persistent mesh safety and closed-cap Floater profiles 0.1.76

Free and surface Floater nodes store an independent lastValidMesh in scene data. Source deletion or invalid parameters reports valid=False while returning an owned copy of that mesh, including after MA/MB reopening. Cache entries are checked for finite points and valid polygon indices. Hot reload adds the cache attribute to already registered types and primes it from any existing last-good plan. Gesture Bevel owns a private copy of its cached template mesh instead of retaining a borrowed DG input object.

Closed curves exactly tracing one support cap now use that cap as a skirt profile. The skirt extends the cap surface outward; it does not ambiguously unfold across the cap-wall corner. Non-cap surface traces retain the existing conforming path. Native regressions use both captured 12-point source caps; all skirts stay quad and no support geometry is modified.

## Floater direction control 0.1.77

Flip Direction reverses the profile height and authored normals for free, cap-outline and conforming-surface floaters, with matching polygon/UV winding. Existing nodes gain the native Boolean attribute during hot refresh; surface helpers inherit it from their parameter node. Preview Cancel restores the prior direction; Apply participates in native Undo/Redo and MA/MB serialization. A captured rotated source cap verifies opposite displacement without support mutation; UI checkbox regression covers Apply.

## Shared mesh safety hot reload 0.1.78

Reload model_pack_mesh_safety before dependent Floater plugin modules, allowing direction/cache schema updates in an already running Maya session. Cold-start geometry tests alone do not exercise imported-helper version skew.

## Shared Mirror manipulator pivot 0.1.79

Mirror Cut/Dup capture the active Move/Rotate/Scale manipulator position before the toolbox changes tools. Multiple selected objects share that world pivot instead of always recentering to the combined bounding box. World/local orientations remain unchanged; without an active transform manipulator the existing selection-center fallback is retained. Regression checks native duplicate positions under transformed parents, capture timing, common plane and mm-to-cm conversion.

## Slide Bevel triangular corner flow 0.1.80

Triangular source sectors position shared support intersections after unfolding the two adjacent face offsets around their common rail. Nearly planar ordinary joins respect native mesh position precision instead of silently substituting a right-angle distance. Multiple-loop stages retain the original source triangle classification, so generated contact triangles cannot change the old collision behavior of unrelated polygon sectors. Captured 20-vertex/17-face source verifies the corrected corner distance, manifold one/two-loop previews, reverse drag, UI publication, native Undo/Redo and MA/MB roundtrip. Earlier narrow-side collision regressions are retained.


## Manual Bevel network normal motion 0.1.150

Manual networks use angle-weighted local bevel normals constrained to the supporting surface tangent space, replacing global parallel-edge constraints. Transverse support rows share one displacement; compact junctions interpolate a constrained field. Local width grips keep those support groups together. Original triangle orientation still bounds the width.

Native offscreen checks: Maya 2025 check_bevel_edit 37/37, check_bevel_bands 12/12, check_draft 115 checks with two existing skips; Maya 2027 check_bevel_edit 37/37. Added a curved supported ring and a read-only capture of the selected 750-face source mesh. The captured network accepts 20% narrowing and 5% widening, moves over 90% of its selected vertices, preserves support group gaps, leaves other vertices unchanged, keeps original triangle orientation, and supports commit/Undo/Redo. Tests also cover local width weights. The working Maya scene was only read to capture mesh data.


## ExtrudeX closed internal contours 0.1.151

The Model Pack host recognizes explicit closed internal edge loops and their filled nearly planar face region. A native polyExtrudeFace inserts an inset ring while retaining the outer contour. The host reuses the supplied drag context, session lifecycle and Undo chunk; the original vendor script and binaries retain their hashes. Internal inset distances stop before cap polygon area inversion. Open border extrusion retains the vendor path.

Native regressions in check_extrudex cover a capped box, the captured 251-edge contour on the user's 504-vertex/541-face source, unchanged outside faces, 251 new inner rim edges, positive cap polygon orientation at an excessive requested distance, native Offset/Extrude mode switching, host activation/finalization, unchanged vendor hashes, invalid chain rejection without scene/selection mutation, one-step Undo/Redo, and MA/MB reopening.

Final checks: Maya 2025 check_extrudex 11/11; Maya 2027 check_extrudex 11/11; Maya 2025 check_draft 115 checks with two existing skips, no failures or errors. The live Maya scene was only read to capture the source mesh and selected contour.
