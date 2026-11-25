from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures" / "sdc"


@pytest.fixture
def fix():
    return FIX
