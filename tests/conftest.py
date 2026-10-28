from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import generate_test_data  # noqa: E402

from networth.config import Config  # noqa: E402
from networth.db import Store  # noqa: E402


@pytest.fixture
def example_xlsx(tmp_path: Path) -> Path:
    """Fictional workbook in the layout of the real NetWorth.xlsx."""
    path = tmp_path / "example_NetWorth.xlsx"
    generate_test_data.write_excel(path, n_rows=150, seed=42)
    return path


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "data" / "networth.sqlite")


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(data_dir=tmp_path / "data", username="tester", password="test-password", password_hash="",
                  secret_key="test-secret", auth_disabled=False, secure_cookies=False, session_days=1)
