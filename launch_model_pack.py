"""Reload the workspace's Model Pack safely in an already open Maya."""
import importlib
from pathlib import Path
import sys


def _reload_module(name):
    module = sys.modules[name]
    template = getattr(module,"TemplateGeometry",None) if name == "model_pack_bevel.geometry" else None
    importlib.reload(module)
    if template is not None:
        # Loaded Maya nodes and cached templates keep this class.
        # Update it in place so a panel reload also fixes saved history.
        for method,value in vars(module.TemplateGeometry).items():
            if method not in ("__dict__","__weakref__","__module__","__doc__"):
                setattr(template,method,value)
        module.TemplateGeometry = template
    return module


def launch():
    root = str(Path(__file__).resolve().parent)
    if root in sys.path:sys.path.remove(root)
    sys.path.insert(0,root)
    for name in ('model_pack_bevel','model_pack_draft','model_pack_selection','model_pack_linear_shift','slide_for_maya'):
        package=sys.modules.get(name)
        if package is not None:
            package.__path__=[str(Path(root)/name)]
            if package.__spec__ is not None:package.__spec__.submodule_search_locations=package.__path__
    old_draft = sys.modules.get('model_pack_fast_draft')
    if old_draft is not None:old_draft.shutdown()
    old_pencil=sys.modules.get('model_pack_pencil_curve')
    if old_pencil is not None:old_pencil.shutdown()
    old_dome=sys.modules.get('model_pack_quad_dome')
    if old_dome is not None:old_dome.shutdown()
    old_floater=sys.modules.get('model_pack_floater')
    if old_floater is not None:old_floater.shutdown()
    old_extrudex=sys.modules.get('model_pack_extrudex')
    if old_extrudex is not None:old_extrudex.shutdown()
    # Finish live histories before any helper module or registry is reloaded.
    old_mirror=sys.modules.get('model_pack_mirror_session')
    if old_mirror is not None:importlib.reload(old_mirror)
    old_toolbox=sys.modules.get('tube_resection')
    if old_toolbox is not None and hasattr(old_toolbox, 'close_toolbox'):
        old_toolbox.close_toolbox()
    if old_toolbox is not None:
        from PySide6 import QtWidgets
        for widget in QtWidgets.QApplication.topLevelWidgets():
            if widget.objectName()==old_toolbox.WORKSPACE_WINDOW_NAME:widget.close()
    old_recurve = sys.modules.get('model_pack_recurve')
    if old_recurve is not None and 'recurve' in sys.modules:
        # A canonical path and an unchanged build label do not mean Python is
        # executing current source. Reload the resident ReSegments helpers too.
        old_recurve.reload_runtime()
    if old_mirror is not None and hasattr(old_mirror,'finish_all'):old_mirror.finish_all()
    old_slide = sys.modules.get("slide_for_maya.ui")
    if old_slide is not None:
        old_slide._on_finalize()
    old_bevel = sys.modules.get("model_pack_bevel.ui")
    if old_bevel is not None:
        old_bevel.shutdown()
    old_unbevel = sys.modules.get("model_pack_bevel.unbevel")
    if old_unbevel is not None:
        old_unbevel.shutdown()
    old_selection = sys.modules.get("model_pack_selection.ui")
    if old_selection is not None:old_selection.shutdown()
    old_linear_shift = sys.modules.get("model_pack_linear_shift.ui")
    if old_linear_shift is not None:old_linear_shift.shutdown()
    for name in ("model_pack_curve_bevel_geometry","model_pack_curve_editor","model_pack_intersection_curve","model_pack_pattern_lattice","model_pack_pattern_waves","model_pack_floater_pattern_geometry","model_pack_floater_pattern","model_pack_crease","model_pack_extrudex","model_pack_paths","model_pack_help","model_pack_theme","slide_for_maya.core","slide_for_maya.bevel","slide_for_maya.multiple","slide_for_maya.edit_session",
                 "slide_for_maya.ui","slide_for_maya","model_pack_bevel.profiles",
                 "model_pack_bevel.graph","model_pack_bevel.collision","model_pack_bevel.corner_rows","model_pack_bevel.surface","model_pack_bevel.geometry","model_pack_bevel.interaction","model_pack_bevel.junctions","model_pack_bevel_junctions","model_pack_bevel.session",
                 "model_pack_bevel.editor","model_pack_bevel.history","model_pack_bevel.band_overlay","model_pack_bevel.values","model_pack_bevel.support_edit","model_pack_bevel.ribbon_edit","model_pack_bevel.support_ribbons","model_pack_bevel.recovered","model_pack_bevel.ui",
                 "model_pack_bevel.unbevel_regions","model_pack_bevel.unbevel_triangles","model_pack_bevel.unbevel_faces","model_pack_bevel.search_cache","model_pack_bevel.unbevel","model_pack_bevel.bevel_detection","model_pack_bevel.seeded_detection","model_pack_bevel.hard_loop_edit","model_pack_bevel",
                 "model_pack_selection.core","model_pack_selection.path_mode","model_pack_selection.ui","model_pack_selection","model_pack_hard_edges","model_pack_edge_flow","model_pack_auto_merge",
                 "model_pack_mirror_geometry","model_pack_legacy_mesh","model_pack_mirror_plane","model_pack_mirror_snap","model_pack_mirror_viewport","model_pack_modifiers","model_pack_mirror_session","model_pack_tube","model_pack_tube_viewport","model_pack_tool_panels",
                 "model_pack_linear_shift.core","model_pack_linear_shift.components","model_pack_linear_shift.planar","model_pack_linear_shift.overlay","model_pack_linear_shift.ui","model_pack_linear_shift","recurve_faces","recurve_colors","recurve_saved_curves","model_pack_recurve",
                 "model_pack_tool_icons","model_pack_nonplanar_hp","model_pack_selection.repeat","recurve_controls","model_pack_control_editor","model_pack_curve_mesh",
                 "model_pack_draft.core","model_pack_draft.cuts","model_pack_draft.cylinder_caps","model_pack_draft.cap_menu",
                 "model_pack_draft.boolean_cleanup","model_pack_draft.snapping","model_pack_draft.hotkey_snaps","model_pack_draft.preview","model_pack_draft.patterns","model_pack_draft.tool","model_pack_fast_draft","model_pack_pencil_fit","model_pack_pencil_curve","model_pack_quad_dome_geometry","model_pack_quad_dome_mesh","model_pack_quad_dome","model_pack_floater_geometry","model_pack_floater_node","model_pack_floater_surface","model_pack_floater_surface_node","model_pack_floater","model_pack_surface_fit","model_pack_surface_fit_ui"):
        if name in sys.modules:
            _reload_module(name)
    bevel_session = sys.modules.get("model_pack_bevel.session")
    if bevel_session is not None:
        bevel_session.refresh_plugin()
    if "tube_resection" in sys.modules:
        importlib.reload(sys.modules["tube_resection"])
    else:
        importlib.import_module("tube_resection")
    modifiers=sys.modules.get('model_pack_modifiers')
    if modifiers is not None:modifiers.refresh_plugin()
    surface=sys.modules.get("model_pack_surface_fit_ui")
    if surface is not None:surface.refresh_nodes()
    from recurve_saved_curves import ensure_plugin, refresh_registered_nodes
    ensure_plugin()
    refresh_registered_nodes()
    from model_pack_curve_mesh import refresh_registered_nodes as refresh_curve_mesh_nodes
    refresh_curve_mesh_nodes()
    sys.modules["tube_resection"].launch_toolbox()


if __name__ == "__main__":
    launch()

