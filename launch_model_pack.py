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
    if root not in sys.path:
        sys.path.insert(0,root)
    # Finish live histories before any helper module or registry is reloaded.
    old_mirror=sys.modules.get('model_pack_mirror_session')
    if old_mirror is not None:importlib.reload(old_mirror)
    old_toolbox=sys.modules.get('tube_resection')
    if old_toolbox is not None:
        from PySide6 import QtWidgets
        for widget in QtWidgets.QApplication.topLevelWidgets():
            if widget.objectName()==old_toolbox.WORKSPACE_WINDOW_NAME:widget.close()
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
    for name in ("model_pack_paths","slide_for_maya.core","slide_for_maya.bevel","slide_for_maya.multiple","slide_for_maya.edit_session",
                 "slide_for_maya.ui","slide_for_maya","model_pack_bevel.profiles",
                 "model_pack_bevel.graph","model_pack_bevel.collision","model_pack_bevel.geometry","model_pack_bevel.interaction","model_pack_bevel.session",
                 "model_pack_bevel.editor","model_pack_bevel.values","model_pack_bevel.ui",
                 "model_pack_bevel.unbevel_regions","model_pack_bevel.unbevel_triangles","model_pack_bevel.unbevel_faces","model_pack_bevel.search_cache","model_pack_bevel.unbevel","model_pack_bevel.bevel_detection","model_pack_bevel.seeded_detection","model_pack_bevel",
                 "model_pack_selection.core","model_pack_selection.ui","model_pack_selection","model_pack_hard_edges",
                 "model_pack_mirror_geometry","model_pack_legacy_mesh","model_pack_mirror_plane","model_pack_mirror_snap","model_pack_mirror_viewport","model_pack_modifiers","model_pack_mirror_session","model_pack_tool_panels",
                 "model_pack_linear_shift.core","model_pack_linear_shift.components","model_pack_linear_shift.overlay","model_pack_linear_shift.ui","model_pack_linear_shift"):
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
    sys.modules["tube_resection"].launch_toolbox()


if __name__ == "__main__":
    launch()
