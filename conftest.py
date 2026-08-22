"""Present so pytest puts the repository root on sys.path.

Under pytest's default prepend import mode the directory containing a test file
becomes its base directory, so `tests/` would go on sys.path and `import common`
would fail. A conftest.py at the root makes the root the base directory instead.

Intentionally empty otherwise.
"""
