from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures" / "sdc"


@pytest.fixture
def fix():
    return FIX


@pytest.fixture(autouse=True)
def _clean_memory_fs():
    import fsspec
    fs = fsspec.filesystem("memory")
    fs.store.clear()
    yield
    fs.store.clear()
