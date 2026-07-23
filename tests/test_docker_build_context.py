from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DockerBuildContextTests(unittest.TestCase):
    def test_sensitive_and_local_files_are_excluded(self):
        rules = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

        required_rules = {
            ".env",
            ".env.*",
            "!.env.example",
            ".git",
            ".github",
            "uploads",
            "__pycache__/",
            "**/__pycache__/",
            "*.pyc",
            ".pytest_cache",
            ".mypy_cache",
            ".coverage",
            "htmlcov",
            ".venv",
            "venv",
            "node_modules",
            ".DS_Store",
            "Thumbs.db",
            ".vscode",
            ".idea",
        }

        self.assertTrue(required_rules.issubset(rules))
        self.assertLess(rules.index(".env.*"), rules.index("!.env.example"))

    def test_runtime_image_uses_narrow_copy_instructions(self):
        dockerfile = (ROOT / "docker" / "app" / "Dockerfile").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("COPY . .", dockerfile)
        self.assertIn("COPY requirements.txt .", dockerfile)
        self.assertIn("COPY app/ app/", dockerfile)
        self.assertIn("COPY worker.py .", dockerfile)


if __name__ == "__main__":
    unittest.main()
