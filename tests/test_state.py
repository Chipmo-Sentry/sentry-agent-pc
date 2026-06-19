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


def _point_state_at(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    """Redirect state_path into a temp dir for save/load tests."""
    from sentry_agent_pc.settings import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "state_path", tmp_path / "state.bin")
    monkeypatch.setattr(state, "get_settings", lambda: settings)
    return settings


def test_save_state_is_atomic_no_stale_tmp(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # After a save the destination exists and no scratch ".state-*.tmp" leaks.
    settings = _point_state_at(tmp_path, monkeypatch)
    state.save_state(state.AgentState(agent_jwt="jwt-atomic"))
    assert settings.state_path.exists()
    leftovers = list(tmp_path.glob(".state-*.tmp"))
    assert leftovers == [], f"leaked scratch files: {leftovers}"
    # And it round-trips.
    assert state.load_state().agent_jwt == "jwt-atomic"


def test_concurrent_saves_never_corrupt(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Two threads hammering save_state must never leave a half-written /
    # undecryptable state file: the final load_state always succeeds.
    import threading

    _point_state_at(tmp_path, monkeypatch)

    errors: list[Exception] = []

    def worker(jwt: str) -> None:
        try:
            for _ in range(50):
                st = state.AgentState(
                    agent_jwt=jwt,
                    cameras=[
                        state.CameraRecord(name=f"{jwt}-cam", ip="10.0.0.1", rtsp_url="rtsp://x"),
                    ],
                )
                state.save_state(st)
        except Exception as e:  # surface any thread error to the assertion
            errors.append(e)

    threads = [
        threading.Thread(target=worker, args=("jwt-A",)),
        threading.Thread(target=worker, args=("jwt-B",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"thread errors: {errors}"
    # Final state must decrypt + validate to one of the two writers' values.
    loaded = state.load_state()
    assert loaded.agent_jwt in {"jwt-A", "jwt-B"}
    assert list(tmp_path.glob(".state-*.tmp")) == []


def test_mutate_state_serialises_read_modify_write(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _point_state_at(tmp_path, monkeypatch)
    state.save_state(state.AgentState(ignored_devices=["1.1.1.1"]))

    def add_device(st: state.AgentState) -> None:
        st.ignored_devices.append("2.2.2.2")

    returned = state.mutate_state(add_device)
    assert "2.2.2.2" in returned.ignored_devices
    assert state.load_state().ignored_devices == ["1.1.1.1", "2.2.2.2"]


def test_persisted_random_secret_handles_concurrent_create(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # If machine.key already exists (race lost), _persisted_random_secret must
    # re-read the winner's value rather than raising FileExistsError.
    from sentry_agent_pc.settings import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "state_path", tmp_path / "state.bin")
    monkeypatch.setattr(state, "get_settings", lambda: settings)
    # Force the persisted-key path (no MachineGuid) by simulating non-Windows.
    monkeypatch.setattr(state.os, "name", "posix")

    first = state._persisted_random_secret()
    second = state._persisted_random_secret()
    assert first == second  # second call re-reads the same file, no exception
