"""State encryption must use a REBOOT-STABLE key.

Regression guard: the key once mixed in uuid.getnode() (a NIC MAC), which
flipped between interfaces on multi-homed/VPN machines → the state file failed
to decrypt on restart → the agent lost its pairing + cameras every reboot.
"""

from __future__ import annotations

from sentry_agent_pc import state


def test_machine_key_is_stable_across_calls() -> None:
    assert state._machine_key() == state._machine_key()


def test_camera_record_matches_by_uuid_or_rtsp_url() -> None:
    a = state.CameraRecord(uuid="u1", name="A", ip="1.1.1.1", rtsp_url="rtsp://a/1")
    a2 = state.CameraRecord(uuid="u1", name="A renamed", ip="9.9.9.9", rtsp_url="rtsp://x/9")
    b = state.CameraRecord(uuid="u2", name="B", ip="2.2.2.2", rtsp_url="rtsp://b/2")
    assert a.matches(a2)  # same uuid → match despite other fields differing
    assert not a.matches(b)
    # Two UNREGISTERED cameras (uuid=None) must NOT all match each other — fall
    # back to rtsp_url so a None-uuid delete can't wipe every unregistered camera.
    n1 = state.CameraRecord(name="N1", ip="3.3.3.3", rtsp_url="rtsp://n/1")
    n2 = state.CameraRecord(name="N2", ip="4.4.4.4", rtsp_url="rtsp://n/2")
    assert not n1.matches(n2)
    assert n1.matches(state.CameraRecord(name="dup", ip="x", rtsp_url="rtsp://n/1"))


def test_clear_pairing_nulls_every_pairing_field() -> None:
    s = state.AgentState(
        agent_jwt="jwt",
        paired_org_id="org",
        default_store_id="store",
        store_name="Дэлгүүр",
    )
    assert s.is_paired
    s.clear_pairing()
    assert not s.is_paired
    # default_store_id used to survive unpair → stale cross-tenant id
    assert (s.agent_jwt, s.paired_org_id, s.default_store_id, s.store_name) == (
        None,
        None,
        None,
        None,
    )


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


def test_legacy_fernet_file_is_migrated(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # H1: a state file written by the OLD machine-key Fernet scheme (no DPAPI
    # prefix) must still load — an upgrade can't drop the existing pairing — and
    # the next save re-seals it (DPAPI on Windows).
    from cryptography.fernet import Fernet

    settings = _point_state_at(tmp_path, monkeypatch)
    plain = state.AgentState(agent_jwt="legacy-jwt").model_dump_json().encode("utf-8")
    settings.state_path.write_bytes(Fernet(state._machine_key()).encrypt(plain))
    assert not settings.state_path.read_bytes().startswith(state._DPAPI_MAGIC)

    loaded = state.load_state()  # migration read
    assert loaded.agent_jwt == "legacy-jwt"

    state.save_state(loaded)  # re-seal
    raw = settings.state_path.read_bytes()
    if state.dpapi.is_available():
        assert raw.startswith(state._DPAPI_MAGIC)  # now DPAPI-sealed
    assert state.load_state().agent_jwt == "legacy-jwt"  # still round-trips


def test_state_file_is_not_plaintext(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # The JWT + camera password must never sit in cleartext on disk.
    settings = _point_state_at(tmp_path, monkeypatch)
    state.save_state(
        state.AgentState(
            agent_jwt="super-secret-jwt",
            cameras=[state.CameraRecord(name="C", ip="1.1.1.1", rtsp_url="rtsp://admin:pw@x")],
        )
    )
    raw = settings.state_path.read_bytes()
    assert b"super-secret-jwt" not in raw
    assert b"admin:pw" not in raw


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
