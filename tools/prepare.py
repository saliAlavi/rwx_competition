"""Build-time step: turn the committed maps into a fast-loading pickle."""
import sys
sys.path.insert(0, "/app")
try:
    from br import index
except ImportError:
    index = None
if index is not None:
    index.prepare("/app/maps", "/app/maps/index.pkl")

# pristine import-time state of sqlglot, for diffing patched imports at run time
import os
import subprocess

env = dict(os.environ, PYTHONPATH="/opt/sqlglot:/app")
subprocess.run([sys.executable, "-m", "br.state", "/app/maps/state.pkl"], env=env, check=True, cwd="/app")
