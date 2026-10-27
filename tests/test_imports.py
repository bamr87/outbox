import ast
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "outbox"


class RelativeImportsTest(unittest.TestCase):
    # zer0-cms loads this package under another name, because its own transactional-outbox app owns
    # `outbox` on the platform's import path (zer0-cms docs/adr/0007-outbox-desk.md). That works only
    # while the package's modules reach each other relatively; one absolute `outbox` import would load
    # the platform's app instead.
    def test_no_module_imports_the_package_by_name(self) -> None:
        offenders = []
        for path in sorted(SRC.glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
                if isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module or ""]
                elif isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                else:
                    continue
                if any(name.split(".")[0] == "outbox" for name in names):
                    offenders.append(f"{path.name}:{node.lineno}")
        self.assertEqual(offenders, [], "import sibling modules relatively (from .belt import ...)")


if __name__ == "__main__":
    unittest.main()
