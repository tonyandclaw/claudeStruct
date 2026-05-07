from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _dockerfile() -> str:
    return (ROOT / "Dockerfile").read_text(encoding="utf-8")


def test_node_builder_uses_glibc_runtime_family():
    text = _dockerfile()

    assert "FROM node:22-bookworm-slim AS ts-builder" in text
    assert "node:22-alpine AS ts-builder" not in text


def test_container_installs_runtime_extras_needed_by_documented_commands():
    text = _dockerfile()

    assert "pip install --no-cache-dir '.[server,openai,smart-context,otel,sentry]'" in text
