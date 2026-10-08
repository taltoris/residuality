"""
test_snapshot.py — integration test for the episodic snapshot pipeline.

Verifies that a snapshot generated against a real (temporary) git repo can be
stored in Qdrant and read back through `latest_snapshot` — the round-trip the
manual "Snapshot" button on Project Control depends on.

The model call is stubbed out: `generate_snapshot` only needs an object with a
`.chat(messages, model=...)` method, so a fake summarizer stands in for OWUI
and no model endpoint has to be running. The Qdrant + embedding services are
the one real dependency; if they are down the tests skip rather than fail,
because their absence is an environment condition, not a code defect.

Run:  python test_snapshot.py        (or: pytest -q test_snapshot.py)
"""

import os
import sys
import json
import time
import shutil
import tempfile
import unittest
from pathlib import Path

# Point the app at a throwaway repos dir before importing it, so nothing here
# touches a real project.
os.environ.setdefault("REPOS_PATH", tempfile.mkdtemp(prefix="res_test_repos_"))

import requests  # noqa: E402  (used to probe service availability)

import snapshot as snap  # noqa: E402


def _services_up() -> bool:
    """True when both Qdrant and the embedding endpoint answer."""
    for url in (snap.QDRANT_URL, snap.EMBED_URL):
        try:
            r = requests.get(url, timeout=3)
            if r.status_code >= 500:
                return False
        except Exception:
            return False
    return True


class FakeSummarizer:
    """Stands in for OWUIClient: returns valid snapshot JSON for any prompt."""

    def chat(self, messages, model=None, **kwargs):
        assert messages and messages[0]["role"] == "user"
        return json.dumps({
            "state":           "Test baseline state.",
            "current_task":    "Wiring up the snapshot pipeline.",
            "key_decisions":   ["Use vanilla JS graph explorer"],
            "open_questions":  ["Is Qdrant guaranteed to be running?"],
            "working":         ["Graph building"],
            "broken":          [],
        })


class TestSnapshotRoundTrip(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.services_ok = _services_up()
        if not cls.services_ok:
            return
        # A minimal real git repo for detect_project_type to classify.
        cls.repo_dir = Path(tempfile.mkdtemp(prefix="res_snap_repo_"))
        (cls.repo_dir / "app.py").write_text(
            "def hello():\n    return 'world'\n\nhello()\n", encoding="utf-8")
        (cls.repo_dir / "README.md").write_text("# demo\n", encoding="utf-8")
        import subprocess
        env = dict(os.environ,
                   GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=cls.repo_dir,
                       check=True, env=env)
        subprocess.run(["git", "add", "."], cwd=cls.repo_dir, check=True, env=env)
        subprocess.run(["git", "commit", "-q", "-m", "baseline"],
                       cwd=cls.repo_dir, check=True, env=env)
        cls.commit_hash = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=cls.repo_dir, check=True,
            capture_output=True, text=env).stdout.strip()

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "repo_dir", None):
            shutil.rmtree(cls.repo_dir, ignore_errors=True)

    def test_generate_and_latest_roundtrip(self):
        if not self.services_ok:
            self.skipTest("Qdrant or embedding endpoint is not reachable")

        project_id = f"unittest_{int(time.time())}"
        try:
            ptype = snap.detect_project_type(self.repo_dir)
            self.assertIn(ptype, ("code", "fiction"))

            generated = snap.generate_snapshot(
                project_id=project_id,
                commit_hash=self.commit_hash,
                commit_message="baseline",
                summary=None,
                project_type=ptype,
                owui_client=FakeSummarizer(),
                summarizer_model="fake-model",
            )
            self.assertIsNotNone(generated, "generate_snapshot returned None")
            self.assertEqual(generated["state"], "Test baseline state.")
            self.assertEqual(generated["commit_hash"], self.commit_hash)
            self.assertEqual(generated["project_id"], project_id)

            stored = snap.store_snapshot(
                project_id=project_id,
                commit_hash=self.commit_hash,
                commit_message="baseline",
                snapshot=generated,
                turn=0,
                timestamp=int(time.time()),
            )
            self.assertTrue(stored, "store_snapshot did not land in Qdrant")

            latest = snap.latest_snapshot(project_id)
            self.assertIsNotNone(latest, "latest_snapshot found nothing")
            self.assertEqual(latest["commit_hash"], self.commit_hash)
            self.assertEqual(latest["state"], "Test baseline state.")
            self.assertEqual(latest["current_task"],
                             "Wiring up the snapshot pipeline.")
            self.assertEqual(latest["project_type"], ptype)

            # The context formatter must render without KeyError for either
            # project type, since Plan injects this into its prompt.
            rendered = snap.format_snapshot_for_context(latest)
            self.assertIn("Test baseline state.", rendered)
        finally:
            # Best-effort cleanup of the per-project collection.
            try:
                requests.delete(
                    f"{snap.QDRANT_URL}/collections/{snap.snapshots_collection(project_id)}",
                    timeout=10,
                )
            except Exception:
                pass


if __name__ == "__main__":
    if not _services_up():
        print("SKIP: Qdrant/embedding endpoints unreachable — "
              "set QDRANT_URL / EMBED_URL to run this test.")
        sys.exit(0)
    unittest.main(verbosity=2)
