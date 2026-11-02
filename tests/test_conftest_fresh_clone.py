"""REVIEW M4 P1: a fresh clone has no out/ (it is git-ignored); the test setup must create it."""

from types import SimpleNamespace

import conftest


def test_basetemp_parent_is_created_in_a_fresh_clone(tmp_path, monkeypatch):
    clone = tmp_path / "fresh-clone"
    clone.mkdir()
    assert not (clone / "out").exists()
    monkeypatch.setattr(conftest, "ROOT", clone)
    config = SimpleNamespace(option=SimpleNamespace(basetemp=None))
    conftest.pytest_configure(config)
    assert (clone / "out").is_dir()
    assert config.option.basetemp == str(clone / "out" / ".pytest-tmp")


def test_explicit_basetemp_is_respected(tmp_path, monkeypatch):
    monkeypatch.setattr(conftest, "ROOT", tmp_path / "unused")
    config = SimpleNamespace(option=SimpleNamespace(basetemp="somewhere-else"))
    conftest.pytest_configure(config)
    assert config.option.basetemp == "somewhere-else" and not (tmp_path / "unused").exists()
