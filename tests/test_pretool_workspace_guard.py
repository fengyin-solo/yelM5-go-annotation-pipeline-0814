import tempfile
import unittest
from pathlib import Path
import sys

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pretool_workspace_guard import bash_path_candidates, inspect_hook  # noqa: E402


class PretoolWorkspaceGuardTest(unittest.TestCase):
    def test_blocks_external_and_private_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "work"
            workspace.mkdir()
            outside = inspect_hook({
                "tool_name": "Read", "cwd": str(workspace),
                "tool_input": {"file_path": "/tmp/secret.go"},
            }, workspace)
            self.assertTrue(outside)
            private = inspect_hook({
                "tool_name": "Bash", "cwd": str(workspace),
                "tool_input": {"command": "git show HEAD:.git/config"},
            }, workspace)
            self.assertTrue(private)

    def test_allows_workspace_and_ignores_heredoc_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "work"
            workspace.mkdir()
            payload = {
                "tool_name": "Bash", "cwd": str(workspace),
                "tool_input": {"command": "cat > main.go <<'EOF'\n// slash comment\nx /= 2\nEOF\ngo test ./..."},
            }
            self.assertEqual([], inspect_hook(payload, workspace))
            self.assertNotIn("/", bash_path_candidates(payload["tool_input"]["command"]))

    def test_macos_var_alias_is_inside(self):
        workspace = Path("/private/var/tmp/work")
        payload = {
            "tool_name": "Read", "cwd": "/var/tmp/work",
            "tool_input": {"file_path": "/var/tmp/work/main.go"},
        }
        self.assertEqual([], inspect_hook(payload, workspace))

    def test_blocks_parent_and_home_expansion(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "work"
            workspace.mkdir()
            for command in (
                "cd .. && pwd", "sed -n 1p $HOME/secret",
                "python3 -c \"open('/tmp/secret').read()\"",
            ):
                payload = {"tool_name": "Bash", "cwd": str(workspace), "tool_input": {"command": command}}
                self.assertTrue(inspect_hook(payload, workspace), command)


if __name__ == "__main__":
    unittest.main()
