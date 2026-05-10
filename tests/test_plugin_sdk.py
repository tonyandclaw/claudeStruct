"""Tests for the Python plugin SDK (W7.4 — PyPI mirror of the
claw-squad TS plugin contract)."""
from __future__ import annotations

from claudestruct.plugin_sdk import (
    PLUGIN_API_VERSION,
    MergedPlugins,
    Plugin,
    SkillContribution,
    SubagentContribution,
    discover_plugins,
    is_plugin,
    load_plugins,
    merge_plugins,
)


def _valid_plugin(**overrides) -> Plugin:
    base = {
        "api_version": PLUGIN_API_VERSION,
        "name": "test-plugin",
        "description": "fixture",
        "subagents": (),
        "skills": (),
    }
    base.update(overrides)
    return Plugin(**base)


# --- is_plugin -----------------------------------------------------


def test_is_plugin_accepts_minimal():
    assert is_plugin(_valid_plugin()) is True


def test_is_plugin_rejects_non_plugin_type():
    assert is_plugin({"name": "foo"}) is False
    assert is_plugin("string") is False
    assert is_plugin(None) is False


def test_is_plugin_rejects_zero_or_negative_api_version():
    assert is_plugin(_valid_plugin(api_version=0)) is False
    assert is_plugin(_valid_plugin(api_version=-1)) is False


def test_is_plugin_rejects_empty_name():
    assert is_plugin(_valid_plugin(name="")) is False


def test_is_plugin_rejects_malformed_subagent_entry():
    p = _valid_plugin(subagents=("not-a-subagent",))  # type: ignore[arg-type]
    assert is_plugin(p) is False


def test_is_plugin_rejects_malformed_skill_entry():
    p = _valid_plugin(skills=({"id": "x"},))  # type: ignore[arg-type]
    assert is_plugin(p) is False


# --- merge_plugins -------------------------------------------------


def test_merge_plugins_keeps_well_formed():
    p1 = _valid_plugin(name="a")
    p2 = _valid_plugin(name="b")
    merged = merge_plugins([p1, p2])
    assert isinstance(merged, MergedPlugins)
    assert {p.name for p in merged.plugins} == {"a", "b"}


def test_merge_plugins_drops_invalid(caplog):
    bad = "not-a-plugin"
    good = _valid_plugin(name="good")
    with caplog.at_level("WARNING", logger="claudestruct.plugin_sdk"):
        merged = merge_plugins([bad, good])  # type: ignore[list-item]
    assert [p.name for p in merged.plugins] == ["good"]
    assert any("Plugin validation" in r.message for r in caplog.records)


def test_merge_plugins_drops_wrong_api_version(caplog):
    """A plugin built against a future SDK major must be skipped
    (with a warning) rather than crashing the host."""
    future = _valid_plugin(name="from-future", api_version=99)
    with caplog.at_level("WARNING", logger="claudestruct.plugin_sdk"):
        merged = merge_plugins([future])
    assert merged.plugins == []
    assert any(
        "api_version" in r.message and "from-future" in r.message
        for r in caplog.records
    )


def test_merge_plugins_dedupes_plugin_names(caplog):
    p1 = _valid_plugin(name="dup", description="first")
    p2 = _valid_plugin(name="dup", description="second")
    with caplog.at_level("WARNING", logger="claudestruct.plugin_sdk"):
        merged = merge_plugins([p1, p2])
    assert len(merged.plugins) == 1
    # First-wins.
    assert merged.plugins[0].description == "first"
    assert any("duplicate plugin name" in r.message for r in caplog.records)


def test_merge_plugins_dedupes_subagent_names_across_plugins(caplog):
    """Two plugins both contribute a subagent named 'security-reviewer'
    — the second one's contribution is dropped."""
    sa1 = SubagentContribution(
        name="security-reviewer", description="d", system_prompt="p",
        provider={"kind": "anthropic"},
    )
    sa2 = SubagentContribution(
        name="security-reviewer", description="d2", system_prompt="p2",
        provider={"kind": "openai"},
    )
    p1 = _valid_plugin(name="a", subagents=(sa1,))
    p2 = _valid_plugin(name="b", subagents=(sa2,))
    with caplog.at_level("WARNING", logger="claudestruct.plugin_sdk"):
        merged = merge_plugins([p1, p2])
    assert len(merged.subagents) == 1
    assert merged.subagents[0].provider == {"kind": "anthropic"}


def test_merge_plugins_dedupes_skill_ids_across_plugins(caplog):
    sk1 = SkillContribution(id="testing", description="d", body="b")
    sk2 = SkillContribution(id="testing", description="other", body="b2")
    p1 = _valid_plugin(name="a", skills=(sk1,))
    p2 = _valid_plugin(name="b", skills=(sk2,))
    with caplog.at_level("WARNING", logger="claudestruct.plugin_sdk"):
        merged = merge_plugins([p1, p2])
    assert len(merged.skills) == 1
    assert merged.skills[0].description == "d"


# --- discover_plugins ----------------------------------------------


def test_discover_plugins_uses_entry_point_group(monkeypatch):
    """The discovery scanner must read the
    `claudestruct.plugins` entry-point group — not a hard-coded
    package-name prefix."""

    class _FakeEntryPoint:
        def __init__(self, name: str, target: object) -> None:
            self.name = name
            self._target = target

        def load(self):
            return self._target

    p = _valid_plugin(name="from-entrypoint")

    class _FakeMetadata:
        @staticmethod
        def entry_points(*, group: str):
            assert group == "claudestruct.plugins"
            return [_FakeEntryPoint("ep1", p)]

    import importlib
    monkeypatch.setattr(importlib, "metadata", _FakeMetadata)

    out = discover_plugins()
    assert [pl.name for pl in out] == ["from-entrypoint"]


def test_discover_plugins_skips_plugin_that_fails_to_load(
    monkeypatch, caplog,
):
    """A broken third-party package must NOT block the rest. The
    scanner logs the error and continues with the next entry."""

    class _FakeEntryPoint:
        def __init__(self, name: str, target: object | None,
                     raises: bool = False) -> None:
            self.name = name
            self._target = target
            self._raises = raises

        def load(self):
            if self._raises:
                raise RuntimeError("simulated import error")
            return self._target

    good = _valid_plugin(name="good")

    class _FakeMetadata:
        @staticmethod
        def entry_points(*, group: str):
            return [
                _FakeEntryPoint("broken", None, raises=True),
                _FakeEntryPoint("good-ep", good),
            ]

    import importlib
    monkeypatch.setattr(importlib, "metadata", _FakeMetadata)

    with caplog.at_level("WARNING", logger="claudestruct.plugin_sdk"):
        out = discover_plugins()
    assert [pl.name for pl in out] == ["good"]
    assert any("simulated import error" in r.message for r in caplog.records)


def test_discover_plugins_skips_non_plugin_resolutions(
    monkeypatch, caplog,
):
    """Entry points that resolve to a non-Plugin object are
    skipped with a warning — protects against a typo in a plugin's
    pyproject.toml pointing at the wrong attribute."""

    class _FakeEntryPoint:
        def __init__(self, name: str, target: object) -> None:
            self.name = name
            self._target = target

        def load(self):
            return self._target

    class _FakeMetadata:
        @staticmethod
        def entry_points(*, group: str):
            return [
                _FakeEntryPoint("not-a-plugin", {"name": "x"}),
            ]

    import importlib
    monkeypatch.setattr(importlib, "metadata", _FakeMetadata)

    with caplog.at_level("WARNING", logger="claudestruct.plugin_sdk"):
        out = discover_plugins()
    assert out == []
    assert any("did not resolve to a Plugin" in r.message for r in caplog.records)


# --- load_plugins (discover + merge) -------------------------------


def test_load_plugins_returns_merged_result(monkeypatch):
    p1 = _valid_plugin(name="loaded")

    class _FakeEntryPoint:
        name = "ep"

        def load(self):
            return p1

    class _FakeMetadata:
        @staticmethod
        def entry_points(*, group: str):
            return [_FakeEntryPoint()]

    import importlib
    monkeypatch.setattr(importlib, "metadata", _FakeMetadata)
    merged = load_plugins()
    assert isinstance(merged, MergedPlugins)
    assert [p.name for p in merged.plugins] == ["loaded"]
