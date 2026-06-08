"""State encryption must use a REBOOT-STABLE key.

Regression guard: the key once mixed in uuid.getnode() (a NIC MAC), which
flipped between interfaces on multi-homed/VPN machines → the state file failed
to decrypt on restart → the agent lost its pairing + cameras every reboot.
"""

from __future__ import annotations

from sentry_agent_pc import state


def test_machine_key_is_stable_across_calls() -> None:
    assert state._machine_key() == state._machine_key()


def test_machine_secret_does_not_use_network_identifier(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # If anything ever reintroduces uuid.getnode(), this changing value would
    # leak into the secret. Assert the secret ignores it.
    import uuid

    monkeypatch.setattr(uuid, "getnode", lambda: 0x111111111111)
    a = state._stable_machine_secret()
    monkeypatch.setattr(uuid, "getnode", lambda: 0x222222222222)
    b = state._stable_machine_secret()
    assert a == b


def test_state_round_trips_cameras(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Save then load must preserve cameras + pairing with the stable key.
    from sentry_agent_pc.settings import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "state_path", tmp_path / "state.bin")
    monkeypatch.setattr(state, "get_settings", lambda: settings)

    st = state.AgentState(
        agent_jwt="jwt-123",
        default_store_id="store-1",
        cameras=[state.CameraRecord(name="C1", ip="192.168.1.64", rtsp_url="rtsp://x")],
    )
    state.save_state(st)
    loaded = state.load_state()
    assert loaded.agent_jwt == "jwt-123"
    assert loaded.is_paired
    assert [c.ip for c in loaded.cameras] == ["192.168.1.64"]
