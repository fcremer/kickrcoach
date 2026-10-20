"""Never mix simulator/CI sessions into a user's personal archive."""
import pytest


@pytest.fixture(autouse=True)
def isolated_archive(tmp_path, monkeypatch):
    monkeypatch.setenv('KICKRCOACH_DATA_DIR', str(tmp_path / 'archive'))
