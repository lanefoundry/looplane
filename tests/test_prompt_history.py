from __future__ import annotations

from looplane.prompt_history import append_prompt_history, load_prompt_history


def test_load_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert load_prompt_history() == []


def test_append_and_load(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    append_prompt_history("hello world")
    append_prompt_history("fix the bug")
    result = load_prompt_history()
    assert result == ["hello world", "fix the bug"]


def test_truncation(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    for i in range(1200):
        append_prompt_history(f"prompt {i}")
    result = load_prompt_history()
    assert len(result) == 500
    assert result[-1] == "prompt 1199"


def test_unicode(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    append_prompt_history("修正這個 bug")
    append_prompt_history("日本語テスト")
    result = load_prompt_history()
    assert result == ["修正這個 bug", "日本語テスト"]


def test_multiline_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    append_prompt_history("line one\nline two\nline three")
    result = load_prompt_history()
    assert result == ["line one\nline two\nline three"]
