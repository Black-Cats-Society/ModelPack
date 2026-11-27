"""Drop this file into Maya's viewport to install or update Model Pack."""
from pathlib import Path
import hashlib
import json
import sys

VERSION='0.1.152-beta'
ANNOTATION='Model Pack by Black Cats Society'
REQUIRED=('launch_model_pack.py','tube_resection.py','model_pack_modifier_nodes.py',
          'model_pack_fast_mirror_node.py','model_pack_paths.py','slide_for_maya/core.py',
          'model_pack_bevel/unBevel1.54.py','model_pack_bevel/recovered.py','model_pack_bevel/junctions.py',
          'model_pack_bevel_junctions.py','model_pack_linear_shift/ui.py',
          'model_pack_selection/ui.py','model_pack_selection/path_mode.py','icons/Model_pack.png','model_pack_recurve.py',
          'recurve.py','recurve_strip.py','recurve_bulk.py','recurve_uv.py','recurve_colors.py',
          'recurve_normals.py','recurve_controls.py','recurve_faces.py','recurve_panel.py','install_recurve.py',
          'recurve_saved_curves.py','model_pack_curve_nodes.py','model_pack_control_editor.py','model_pack_curve_mesh.py','model_pack_curve_mesh_node.py',
          'icons/resegments.svg','icons/control_curve.svg','icons/ngons.svg',
          'icons/triangles.svg','icons/apply.svg','icons/cancel.svg',
          'icons/resegments_active.svg','icons/control_curve_active.svg',
          'model_pack_fast_draft.py','model_pack_draft/__init__.py','model_pack_draft/core.py',
          'model_pack_draft/patterns.py','model_pack_draft/cuts.py','model_pack_draft/tool.py','model_pack_draft/preview.py',
          'model_pack_draft/cylinder_caps.py','model_pack_draft/cap_menu.py','model_pack_draft/snapping.py',
          'model_pack_draft/boolean_cleanup.py',
          'model_pack_draft/hotkey_snaps.py',
          'model_pack_update_bridge.py','model_pack_update_plugin.py',
          'icons/fast_draft.svg','icons/draft_cube.svg','icons/draft_cylinder.svg',
          'model_pack_edge_flow.py','icons/edge_flow.svg','icons/edge_flow_active.svg',
          'model_pack_auto_merge.py','icons/auto_merge.svg','icons/auto_merge_active.svg',
          'model_pack_pencil_fit.py','model_pack_pencil_curve.py','icons/pencil_curve.svg','icons/pencil_curve_active.svg',
          'model_pack_quad_dome_geometry.py','model_pack_quad_dome_mesh.py','model_pack_quad_dome.py',
          'icons/quad_dome.svg','icons/quad_dome_active.svg',
          'model_pack_floater_geometry.py','model_pack_floater_node.py','model_pack_floater.py',
          'model_pack_floater_surface.py','model_pack_floater_surface_node.py',
          'model_pack_curve_editor.py','model_pack_curve_bevel_geometry.py',
          'model_pack_floater_pattern.py','model_pack_floater_pattern_geometry.py','model_pack_pattern_lattice.py','model_pack_pattern_waves.py',
          'icons/floater.svg','icons/floater_active.svg')


REQUIRED += ('model_pack_surface_fit.py','model_pack_surface_fit_node.py','model_pack_surface_fit_ui.py','icons/surface_fit.svg')
REQUIRED += ('model_pack_nonplanar_hp.py',)

def validate_package(folder):
    folder=Path(folder).resolve()
    missing=[name for name in REQUIRED if not (folder/name).is_file()]
    if missing:raise RuntimeError('Model Pack: extract the entire archive. Missing: '+', '.join(missing))
    manifest=folder/'package_manifest.json'
    if manifest.is_file() and not (folder/'.git').exists():
        info=json.loads(manifest.read_text(encoding='utf-8'))
        for name,digest in info['files'].items():
            path=(folder/name).resolve()
            if not path.is_relative_to(folder):raise RuntimeError('Model Pack: invalid manifest path.')
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
                raise RuntimeError('Model Pack: damaged or modified file: '+name+'. Extract the archive again.')
    return folder


def shelf_command(folder):
    folder=Path(folder).resolve()
    return ('import sys, runpy\n'
            f'p = {str(folder)!r}\n'
            'if p in sys.path: sys.path.remove(p)\n'
            'sys.path.insert(0, p)\n'
            f'runpy.run_path({str(folder/"launch_model_pack.py")!r}, run_name="__main__")')


def module_text(folder):
    location=Path(folder).resolve().as_posix()
    # Maya can load a Python plug-in by a short absolute path, but its scene
    # loader cannot reliably find it by name through that same directory.
    if not location.isascii():
        raise RuntimeError('Model Pack: scene history requires an ASCII folder path. Extract to C:/MayaTools/ModelPack and install again.')
    # The rest of the first line is the path; do not quote paths with spaces.
    return (f'+ ModelPack {VERSION.split("-")[0]} {location}\n'
            'scripts: .\n'
            'plug-ins: .\n'
            'PYTHONPATH +:= .\n'
            'MAYA_PLUG_IN_PATH +:= model_pack_bevel\n')


def install(folder=None,open_panel=True):
    import maya.cmds as cmds
    import maya.mel as mel
    if cmds.about(batch=True):raise RuntimeError('Model Pack: run the installer inside Maya.')
    try:
        import PySide6,shiboken6,numpy  # Maya supplies these dependencies.
    except ImportError as error:
        raise RuntimeError('Model Pack requires Maya with PySide6, shiboken6 and NumPy.') from error
    folder=validate_package(folder or Path(__file__).resolve().parent)
    shelf_top=mel.eval('$tmpModelPackShelf = $gShelfTopLevel')
    shelf=cmds.tabLayout(shelf_top,q=True,selectTab=True)
    if not shelf:raise RuntimeError('Model Pack: select a Shelf tab before installation.')
    children=cmds.shelfLayout(shelf,q=True,childArray=True) or []
    button=next((item for item in children if cmds.objectTypeUI(item)=='shelfButton'
                 and cmds.shelfButton(item,q=True,annotation=True)==ANNOTATION),None)
    module=Path(cmds.internalVar(userAppDir=True))/'modules'/'ModelPack.mod'
    text=module_text(folder)
    module.parent.mkdir(parents=True,exist_ok=True)
    if not module.is_file() or module.read_text(encoding='utf-8')!=text:
        temporary=module.with_suffix('.mod.tmp');temporary.write_text(text,encoding='utf-8');temporary.replace(module)
    options=dict(label='Model Pack',imageOverlayLabel='MP',annotation=ANNOTATION,
                 image=str(folder/'icons'/'Model_pack.png'),sourceType='python',command=shelf_command(folder))
    if button:cmds.shelfButton(button,e=True,**options)
    else:button=cmds.shelfButton(parent=shelf,**options)
    if open_panel:exec(options['command'],{'__name__':'__main__'})
    cmds.inViewMessage(amg='Model Pack installed. Save the shelf: Shelf → Save All Shelves.',pos='topCenter',fade=True)
    return button


def onMayaDroppedPythonFile(*args):
    return install()


if __name__=='__main__':onMayaDroppedPythonFile()
