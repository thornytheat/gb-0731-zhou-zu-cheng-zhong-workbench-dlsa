import shutil

import pytest
from fastapi.testclient import TestClient

from app.config import ROOT, Settings
from app.main import create_app
from app.modules.m7_batches.pipeline import run_pipeline
from app.sources import read_sources

LATE_CERTIFICATE = (
    "证书,轮道,生效,失效,增益,偏置千克\n"
    "L-旧,L,2026-11-20T20:00:00.000+08:00,2026-11-20T20:00:00.100+08:00,0.995,0\n"
    "L-1120,L,2026-11-20T12:00:00.100Z,2026-11-20T20:07:00.000+08:00,1,10\n"
    "R-1120,R,2026-11-20T20:00:00.000+08:00,2026-11-20T20:06:02.000+08:00,1,-5\n"
    "R-借表,R,2026-11-20T12:01:00.500Z,2026-11-20T20:01:00.800+08:00,1.003,22\n"
)


def append_late_certificate(directory):
    """把迟到的新表证书追加到已有证书文件，返回 (原字节, 新字节)。"""
    path = directory / "certificates.csv"
    original = path.read_bytes()
    late = LATE_CERTIFICATE + "R-补录,R,2026-11-20T20:01:00.600+08:00,2026-11-20T20:05:00.000+08:00,1.01,30\n"
    path.write_bytes(late.encode("gb18030"))
    return original, path.read_bytes()


@pytest.fixture(scope="session")
def dirty():
    return read_sources(ROOT / "sample_data", "dirty")


@pytest.fixture(scope="session")
def dirty_result(dirty):
    return run_pipeline(dirty)


@pytest.fixture(scope="session")
def clean_result():
    return run_pipeline(read_sources(ROOT / "sample_data", "clean"))


@pytest.fixture
def settings(tmp_path):
    return Settings(tmp_path / "sources", tmp_path / "weigh.sqlite3")


@pytest.fixture
def client(settings):
    shutil.copytree(ROOT / "sample_data", settings.source_dir)
    with TestClient(create_app(settings)) as test_client:
        yield test_client
