"""Import every module so a missing dependency or import fails CI, not startup."""

import importlib
import importlib.util
import os
import pathlib
import sys

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
os.environ.setdefault("WEATHERFLOW_COLLECTOR_API_TOKEN", "test-token")

MODULES = sorted(
    ".".join(p.relative_to(SRC).with_suffix("").parts)
    for p in SRC.rglob("*.py")
    if p.name != "weatherflow-collector.py" and p.name != "__init__.py"
)


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name):
    importlib.import_module(name)


def test_entrypoint_imports():
    spec = importlib.util.spec_from_file_location(
        "weatherflow_collector_main", SRC / "weatherflow-collector.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert hasattr(module, "setup_app")
