"""Plugin SDK (W7.4 — Python mirror of the claw-squad plugin contract).

Lets third parties contribute new task subagents and skills to
``cs`` without forking the CLI. A plugin is a Python distribution
that registers an ``claudestruct.plugins`` entry point — same
discovery mechanism setuptools/Hatch/Poetry use for everything
else, so ``pip install claudestruct-plugin-foo`` is automatically
visible.

Entry-point shape (in the plugin's ``pyproject.toml``):

    [project.entry-points."claudestruct.plugins"]
    foo = "claudestruct_plugin_foo:plugin"

The right-hand side resolves to a :class:`Plugin` instance.

Why mirror the TS contract instead of inventing a new one?
A team that ships a TS plugin already understands the shape;
keeping the Python one identical (modulo language conventions)
means a plugin author writes one mental model. The two SDKs
intentionally use the same field names where possible.

What plugins CANNOT do (for the same reason as the TS side):
- Replace the dev / review / plan / debug task prompts. Those are
  core; a plugin flipping a task-mode prompt breaks every other
  plugin's expectations.
- Hook the network call directly. Use a skill or subagent.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("claudestruct.plugin_sdk")


# Bumped only on breaking SDK changes. Plugins declaring a different
# major are skipped at load time with a warning. Mirror of
# `claw-squad/src/plugins.ts:PLUGIN_API_VERSION`.
PLUGIN_API_VERSION = 1


@dataclass(frozen=True)
class SubagentContribution:
    """A new subagent the plugin contributes to the catalogue.

    ``provider`` is the provider config dict; the host wires the
    actual Provider object at boot so plugins don't need to bundle
    SDKs.
    """
    name: str
    description: str
    system_prompt: str
    provider: dict[str, Any]


@dataclass(frozen=True)
class SkillContribution:
    """A skill the plugin contributes. Same shape as a local skill
    minus ``path`` (the host fills it in to point at the plugin's
    package directory)."""
    id: str
    description: str
    body: str
    apply_to: tuple[str, ...] = ()


@dataclass(frozen=True)
class Plugin:
    """The object a plugin's entry point must resolve to."""
    api_version: int
    name: str
    description: str = ""
    subagents: tuple[SubagentContribution, ...] = ()
    skills: tuple[SkillContribution, ...] = ()


@dataclass
class MergedPlugins:
    """Result of merging a discovered plugin list. The host reads
    these flat collections rather than iterating plugins directly."""
    plugins: list[Plugin] = field(default_factory=list)
    subagents: list[SubagentContribution] = field(default_factory=list)
    skills: list[SkillContribution] = field(default_factory=list)


def is_plugin(obj: Any) -> bool:
    """Validate an object as a Plugin without raising. Returns
    True iff every required field is present and well-typed."""
    if not isinstance(obj, Plugin):
        return False
    if not isinstance(obj.api_version, int) or obj.api_version <= 0:
        return False
    if not isinstance(obj.name, str) or not obj.name:
        return False
    if obj.subagents is not None and not all(
        isinstance(s, SubagentContribution) for s in obj.subagents
    ):
        return False
    if obj.skills is not None and not all(
        isinstance(s, SkillContribution) for s in obj.skills
    ):
        return False
    return True


def merge_plugins(plugins: Iterable[Plugin]) -> MergedPlugins:
    """Validate + dedupe + flatten a list of plugins.

    Dedupe rules (mirror the TS side):
      - Plugin names are unique across the load. A duplicate
        plugin name is dropped with a warning (first one wins).
      - Subagent names are unique across plugins. A duplicate
        subagent across two plugins is dropped with a warning.
      - Skill ids are unique across plugins. Same dedup rule.
    """
    out = MergedPlugins()
    seen_plugins: set[str] = set()
    seen_subagents: set[str] = set()
    seen_skills: set[str] = set()
    for p in plugins:
        if not is_plugin(p):
            log.warning(
                "skipping plugin %r: failed Plugin validation",
                getattr(p, "name", "<unknown>"),
            )
            continue
        if p.api_version != PLUGIN_API_VERSION:
            log.warning(
                "skipping plugin %r: api_version=%d does not match host %d",
                p.name, p.api_version, PLUGIN_API_VERSION,
            )
            continue
        if p.name in seen_plugins:
            log.warning(
                "skipping duplicate plugin name %r (first one wins)",
                p.name,
            )
            continue
        seen_plugins.add(p.name)
        out.plugins.append(p)
        for s in p.subagents:
            if s.name in seen_subagents:
                log.warning(
                    "skipping duplicate subagent name %r from plugin %r",
                    s.name, p.name,
                )
                continue
            seen_subagents.add(s.name)
            out.subagents.append(s)
        for sk in p.skills:
            if sk.id in seen_skills:
                log.warning(
                    "skipping duplicate skill id %r from plugin %r",
                    sk.id, p.name,
                )
                continue
            seen_skills.add(sk.id)
            out.skills.append(sk)
    return out


# --- Discovery -----------------------------------------------------


_ENTRY_POINT_GROUP = "claudestruct.plugins"


def discover_plugins(*, group: str = _ENTRY_POINT_GROUP) -> list[Plugin]:
    """Read the ``claudestruct.plugins`` entry-point group and
    instantiate each plugin. Errors during a single plugin's load
    are logged and the plugin is skipped — one broken third-party
    package must not block the rest.
    """
    try:
        from importlib import metadata as importlib_metadata
    except ImportError:  # pragma: no cover — Python < 3.8
        return []

    try:
        eps = importlib_metadata.entry_points(group=group)
    except TypeError:
        # Python 3.9 and earlier: entry_points() returns a dict
        # without the `group=` kwarg.
        eps = importlib_metadata.entry_points().get(group, [])  # type: ignore[attr-defined]

    plugins: list[Plugin] = []
    for ep in eps:
        try:
            obj = ep.load()
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "plugin entry point %r failed to load: %s: %s",
                ep.name, type(exc).__name__, exc,
            )
            continue
        if not is_plugin(obj):
            log.warning(
                "plugin entry point %r did not resolve to a Plugin "
                "(got %s); skipping",
                ep.name, type(obj).__name__,
            )
            continue
        plugins.append(obj)
    return plugins


def load_plugins() -> MergedPlugins:
    """One-shot helper: discover + merge.

    Use this from `cs` startup. The result is meant to be cached
    for the process lifetime (entry-point discovery hits importlib
    metadata on every call).
    """
    return merge_plugins(discover_plugins())


__all__ = [
    "PLUGIN_API_VERSION",
    "Plugin",
    "SubagentContribution",
    "SkillContribution",
    "MergedPlugins",
    "is_plugin",
    "merge_plugins",
    "discover_plugins",
    "load_plugins",
]
