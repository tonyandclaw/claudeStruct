from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _dockerfile() -> str:
    return (ROOT / "Dockerfile").read_text(encoding="utf-8")


def _docker_workflow() -> str:
    return (
        ROOT / ".github" / "workflows" / "docker.yml"
    ).read_text(encoding="utf-8")


def _ci_workflow() -> str:
    return (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")


def test_node_builder_uses_glibc_runtime_family():
    text = _dockerfile()

    assert "FROM node:22-bookworm-slim AS ts-builder" in text
    assert "node:22-alpine AS ts-builder" not in text


def test_container_installs_runtime_extras_needed_by_documented_commands():
    text = _dockerfile()

    assert "pip install --no-cache-dir '.[server,openai,smart-context,otel,sentry]'" in text


# --- Supply-chain gates in docker.yml (W4.4 follow-up) --------------


def test_docker_workflow_generates_sbom():
    """The release pipeline must publish a CycloneDX SBOM alongside
    the image so downstream consumers can audit our deps. Locking
    the workflow step prevents a refactor from silently dropping it."""
    text = _docker_workflow()
    assert "anchore/sbom-action" in text, (
        "SBOM step missing — the W4.4 follow-up requires anchore/sbom-action"
    )
    assert "cyclonedx-json" in text, (
        "SBOM format must be cyclonedx-json (the format we standardise on)"
    )


def test_docker_workflow_uploads_sbom_artefact():
    """SBOM must be persisted as a workflow artefact so it's
    downloadable from the Actions UI for security review."""
    text = _docker_workflow()
    assert "actions/upload-artifact" in text
    assert "sbom" in text.lower()


def test_docker_workflow_runs_trivy_with_failing_gate():
    """Trivy must run on every build AND fail the workflow on
    CRITICAL/HIGH findings — otherwise the gate is decorative."""
    text = _docker_workflow()
    assert "aquasecurity/trivy-action" in text
    assert "exit-code: '1'" in text or 'exit-code: "1"' in text, (
        "Trivy must fail the workflow on findings (exit-code: '1')"
    )
    assert "CRITICAL" in text and "HIGH" in text, (
        "Trivy severity gate should at least cover CRITICAL and HIGH"
    )


def test_docker_workflow_uploads_trivy_sarif_to_security_tab():
    """SARIF upload makes findings visible in the GitHub Security
    tab so they survive even when the workflow run is GC'd."""
    text = _docker_workflow()
    assert "github/codeql-action/upload-sarif" in text
    assert "trivy-results.sarif" in text


def test_docker_workflow_security_events_permission_present():
    """Uploading SARIF requires `security-events: write`; without it
    the upload step silently no-ops."""
    text = _docker_workflow()
    assert "security-events: write" in text


# --- Secret scan in CI --------------------------------------------


def test_ci_workflow_runs_gitleaks_secret_scan():
    """A committed credential is 1000× worse than any test failure;
    gitleaks must run on every PR + push so a bad commit never
    makes it past review."""
    text = _ci_workflow()
    assert "gitleaks/gitleaks-action" in text


def test_ci_workflow_secret_scan_uses_full_history():
    """gitleaks needs `fetch-depth: 0` to scan every commit the PR
    introduces — the default depth-1 only sees the merge commit
    and would miss a secret added in a middle commit."""
    text = _ci_workflow()
    assert "fetch-depth: 0" in text
