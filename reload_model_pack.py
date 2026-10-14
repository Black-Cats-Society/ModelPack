"""Run this file in Maya to reopen Model Pack from its installed folder."""
from pathlib import Path
import sys, runpy
folder = Path(__file__).resolve().parent
if str(folder) in sys.path: sys.path.remove(str(folder))
sys.path.insert(0, str(folder))
runpy.run_path(str(folder / "launch_model_pack.py"), run_name="__main__")
