"""Discover all backend tests once, including imported TestCase classes only once."""
import argparse
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def unique_cases(suite):
    cases = {}
    def visit(item):
        if isinstance(item, unittest.TestSuite):
            for child in item:
                visit(child)
        else:
            cases.setdefault(item.id(), item)
    visit(suite)
    return cases


class ImmediateErrors(unittest.TextTestResult):
    def addError(self, test, err):
        super().addError(test, err)
        self.stream.writeln(self.errors[-1][1])

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.stream.writeln(self.failures[-1][1])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pattern", default="test_*.py")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern=args.pattern, top_level_dir=str(ROOT))
    cases = unique_cases(suite)
    print(f"Discovered {suite.countTestCases()} test entries; {len(cases)} unique test IDs", flush=True)
    if args.list:
        print("\n".join(cases))
        return 0
    if not cases:
        print("No tests matched; refusing a successful empty run.", file=sys.stderr)
        return 2
    result = unittest.TextTestRunner(verbosity=2, resultclass=ImmediateErrors).run(unittest.TestSuite(cases.values()))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
