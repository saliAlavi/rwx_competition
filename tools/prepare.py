"""Build-time step: turn the committed maps into a fast-loading pickle."""
import sys
sys.path.insert(0, "/app")
try:
    from br import index
except ImportError:
    index = None
if index is not None:
    index.prepare("/app/maps", "/app/maps/index.pkl")
