import sys


def test_project_uses_python_312() -> None:
    assert sys.version_info[:2] == (3, 12)
