import pytest

from trader.journal import J


@pytest.fixture(autouse=True)
def _journal_to_tmp(tmp_path):
    """Tests must not write into the real data/journal."""
    J.dir, J._day, J._fh = tmp_path / "journal", "", None
    yield
    if J._fh:
        J._fh.close()
    J._fh = None
