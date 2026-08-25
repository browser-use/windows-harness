from __future__ import annotations

from windows_harness import telemetry


def test_uses_windows_harness_posthog_project() -> None:
    assert telemetry.POSTHOG_HOST == "https://eu.i.posthog.com"
    assert telemetry.POSTHOG_KEY == (
        "phc_m63HdH4EBrJQap6pHqiSb4vi49tcFk8j4y2ipi5EMsd4"
    )


def test_status_creates_anonymous_config(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WINDOWS_HARNESS_HOME", str(tmp_path / "config"))

    status = telemetry.status()
    path = tmp_path / "config" / "telemetry.json"

    assert status["enabled"] is True
    assert status["install_id"]
    assert path.exists()


def test_environment_and_config_can_disable_telemetry(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WINDOWS_HARNESS_HOME", str(tmp_path))
    monkeypatch.setenv("WINDOWS_HARNESS_TELEMETRY", "0")
    assert telemetry.is_enabled() is False

    monkeypatch.delenv("WINDOWS_HARNESS_TELEMETRY")
    telemetry.set_enabled(False)
    assert telemetry.is_enabled() is False

    telemetry.set_enabled(True)
    assert telemetry.is_enabled() is True


def test_capture_has_no_channel_for_user_data(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WINDOWS_HARNESS_HOME", str(tmp_path))
    sent = []
    monkeypatch.setattr(telemetry, "_send", sent.append)

    telemetry.capture_cli("unknown-user-value", True, 1.234)

    payload = sent[0]
    assert payload["event"] == "windows_harness_cli"
    properties = payload["properties"]
    assert properties["command"] == "python"
    assert properties["success"] is True
    assert properties["duration_seconds"] == 1.23
    assert set(properties) == {
        "$geoip_disable",
        "$process_person_profile",
        "agent",
        "command",
        "duration_seconds",
        "machine",
        "os",
        "python_version",
        "success",
        "windows_harness_version",
    }
