"""Generated documentation stays in step with its sources (fleet UPS-DOC-03/05).

docs/reference/cli.md is rendered from the argparse parser and docs/README.md's
index from each document's summary line; both are regenerated here and compared,
offline, so a new flag or document cannot land with stale docs.
"""

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_tool(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GeneratedDocsTests(unittest.TestCase):
    def test_cli_reference_matches_the_parser(self):
        self.assertTrue(
            load_tool("gen_cli_reference").is_current(ROOT),
            "docs/reference/cli.md is stale: run `python3 tools/gen_cli_reference.py --write`",
        )

    def test_docs_index_is_current(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "docs_index.py"), "--check", "--root", str(ROOT)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
