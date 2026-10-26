import os

import pytest

from ion.artifacts import ArtifactStore


def test_artifact_store_rejects_symlinked_artifact(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    artifact = store.put(b"safe", "text")
    outside = tmp_path / "outside"
    outside.write_bytes(b"secret")
    (store.root / artifact.artifact_id).unlink()
    os.symlink(outside, store.root / artifact.artifact_id)
    with pytest.raises(ValueError, match="symlink"):
        store.read(artifact.artifact_id)


def test_artifact_store_rejects_symlinked_completeness_metadata(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    artifact = store.put(b"safe", "text")
    outside = tmp_path / "outside-state"
    outside.write_bytes(b"1")
    metadata = store.root / ".metadata" / artifact.artifact_id
    metadata.unlink()
    os.symlink(outside, metadata)
    with pytest.raises(ValueError, match="symlink"):
        store.is_complete(artifact.artifact_id)
