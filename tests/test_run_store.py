import json
import multiprocessing
import os
import stat
from pathlib import Path

import pytest

from deepresearch_cli.persistence import (
    IntegrityError,
    PersistenceValidationError,
    RunAlreadyExistsError,
    RunBusyError,
    RunStore,
    UnsafePathError,
)


def manifest(run_id="run-store"):
    return {
        "schema_version": "2",
        "runtime": "config-workflow",
        "run_id": run_id,
        "created_at": "2026-08-13T00:00:00Z",
        "context": {
            "query": "test",
            "language": "en",
            "mode": "quick",
            "output_format": "markdown",
        },
        "workflow": {},
        "nodes": [],
        "definition_hash": "sha256:" + "0" * 64,
    }


def started(instance="research-a", attempt=1, inputs=None):
    return {
        "type": "step_started",
        "step_id": "research",
        "node_id": "research",
        "instance_id": instance,
        "scope": {},
        "attempt": attempt,
        "inputs": inputs or [],
    }


def artifact(base, instance="research-a", attempt=1):
    return {
        "port": "evidence",
        "type": "evidence",
        "media_type": "application/json",
        "path": base["path"],
        "sha256": base["sha256"],
        "scope": {},
        "mode": "state",
        "step_id": "research",
        "instance_id": instance,
    }


def finished(refs, instance="research-a", attempt=1, outcome="succeeded"):
    return {
        "type": "step_finished",
        "step_id": "research",
        "node_id": "research",
        "instance_id": instance,
        "scope": {},
        "attempt": attempt,
        "outcome": outcome,
        "artifact_refs": refs,
        "diagnostics_ref": f"attempts/{instance}/attempt-{attempt}",
        "error": None,
        "validation_warnings": [],
    }


def publish(store, run_id="run-store", instance="research-a", attempt=1):
    layout = store.prepare_attempt(run_id, instance, attempt)
    (layout.staging_dir / "evidence.json").write_text('{"ok": true}\n')
    candidate = store.freeze_staging_candidate(run_id, instance, attempt)
    store.seal_candidate(run_id, instance, attempt, candidate)
    snapshot = store.snapshot_candidate(run_id, instance, attempt, candidate)
    return store.publish_artifacts(run_id, instance, attempt, candidate, snapshot)[0]


def _hold_lock(root, run_id, ready, release):
    store = RunStore(root)
    with store.execution_session(run_id):
        ready.set()
        release.wait(timeout=10)


def test_create_run_is_immutable_and_cannot_replace_existing(tmp_path):
    store = RunStore(tmp_path / "runs")
    run_dir = store.create_run(manifest())

    assert store.load_manifest("run-store")["runtime"] == "config-workflow"
    assert (run_dir / "journal.jsonl").read_bytes() == b""
    assert not os.stat(run_dir / "run.json").st_mode & stat.S_IWUSR
    with pytest.raises(RunAlreadyExistsError):
        store.create_run(manifest())


@pytest.mark.parametrize(
    "update",
    [
        {"schema_version": "1"},
        {"runtime": "graph-runtime"},
    ],
)
def test_old_manifests_are_explicitly_rejected(tmp_path, update):
    store = RunStore(tmp_path / "runs")
    value = manifest()
    value.update(update)
    with pytest.raises(PersistenceValidationError):
        store.create_run(value)


def test_new_events_and_artifact_contract_round_trip(tmp_path):
    store = RunStore(tmp_path / "runs")
    store.create_run(manifest())
    start = store.append_event("run-store", started())
    ref = artifact(publish(store))
    end = store.append_event("run-store", finished([ref]))

    assert start["seq"] == 1
    assert start["recorded_at"].endswith("Z")
    assert end["seq"] == 2
    assert store.load_run("run-store").events == [start, end]
    assert store.validate_artifact_ref("run-store", ref).read_text() == '{"ok": true}\n'


def test_event_rejects_invalid_recorded_at(tmp_path):
    store = RunStore(tmp_path / "runs")
    store.create_run(manifest())
    event = started()
    event["recorded_at"] = "not-a-timestamp"

    with pytest.raises(PersistenceValidationError, match="recorded_at"):
        store.append_event("run-store", event)


def test_step_input_must_reference_an_earlier_committed_artifact(tmp_path):
    store = RunStore(tmp_path / "runs")
    store.create_run(manifest())
    ref = artifact(publish(store))

    with pytest.raises(IntegrityError, match="earlier committed"):
        store.append_event("run-store", started("writer-a", inputs=[ref]))


def test_tampered_artifact_blocks_status_and_future_commits(tmp_path):
    store = RunStore(tmp_path / "runs")
    store.create_run(manifest())
    store.append_event("run-store", started())
    ref = artifact(publish(store))
    store.append_event("run-store", finished([ref]))
    path = store.run_dir("run-store") / ref["path"]
    path.chmod(0o600)
    path.write_text("tampered", encoding="utf-8")

    with pytest.raises(IntegrityError, match="hash mismatch"):
        store.load_run("run-store")
    with pytest.raises(IntegrityError, match="hash mismatch"):
        store.append_event("run-store", {"type": "run_finished", "status": "completed"})


def test_artifact_ref_rejects_path_escape_and_shape_expansion(tmp_path):
    store = RunStore(tmp_path / "runs")
    store.create_run(manifest())
    store.append_event("run-store", started())
    ref = artifact(publish(store))
    ref["path"] = "../outside"
    with pytest.raises(UnsafePathError):
        store.append_event("run-store", finished([ref]))

    ref = artifact({"path": "artifacts/research-a/attempt-1/evidence.json", "sha256": "sha256:" + "0" * 64})
    ref["extra"] = "not allowed"
    with pytest.raises(PersistenceValidationError, match="Artifact contract"):
        store.append_event("run-store", finished([ref]))


def test_torn_journal_is_rejected(tmp_path):
    store = RunStore(tmp_path / "runs")
    run_dir = store.create_run(manifest())
    journal = run_dir / "journal.jsonl"
    journal.write_text(json.dumps({"type": "step_started"}), encoding="utf-8")
    with pytest.raises(IntegrityError, match="torn final record"):
        store.load_journal("run-store")


def test_event_sequence_rejects_finish_without_start_and_terminal_extension(tmp_path):
    store = RunStore(tmp_path / "runs")
    store.create_run(manifest())
    with pytest.raises(PersistenceValidationError, match="active attempt"):
        store.append_event("run-store", finished([]))

    store.append_event("run-store", {"type": "run_finished", "status": "completed"})
    with pytest.raises(PersistenceValidationError, match="run_finished"):
        store.append_event("run-store", started())


def test_execution_session_is_exclusive_across_processes(tmp_path):
    root = tmp_path / "runs"
    store = RunStore(root)
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_lock, args=(str(root), "run-store", ready, release)
    )
    process.start()
    try:
        assert ready.wait(timeout=10)
        with pytest.raises(RunBusyError):
            with store.execution_session("run-store"):
                pass
    finally:
        release.set()
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
    assert process.exitcode == 0
