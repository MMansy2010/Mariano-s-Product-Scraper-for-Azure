"""Global test configuration and fixtures for Mariano's collector test suite."""
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import kroger_collector


@pytest.fixture(autouse=True)
def isolate_test_evidence_and_checkpoint(tmp_path, monkeypatch):
    """Ensure all tests operate in isolated directories and never write to production data/evidence."""
    test_evidence = tmp_path / "test_evidence"
    test_checkpoint = tmp_path / "test_checkpoints"
    test_evidence.mkdir(parents=True, exist_ok=True)
    test_checkpoint.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(kroger_collector, "EVIDENCE_DIR", test_evidence)
    monkeypatch.setattr(kroger_collector, "CHECKPOINT_DIR", test_checkpoint)
