import sys

# sys.path entries can get held open, and Windows can't update an open .exe.
sys.path[:] = [p for p in sys.path if not p.lower().endswith('.exe')]
