from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from workbench.api import _read_task_script


class TaskScriptProjectionTests(unittest.TestCase):
    def test_reads_only_the_task_owned_locked_script(self):
        with TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "runtime" / "test-tmp") as temp:
            root = Path(temp)
            path = root / "runtime" / "workbench" / "tasks" / "task-1" / "inputs" / "final-script.txt"
            path.parent.mkdir(parents=True)
            path.write_text("本次最终文案", encoding="utf-8")
            task = {"task_id": "task-1", "brief": {"final_script": {
                "relative_path": "inputs/final-script.txt",
            }}}
            self.assertEqual(_read_task_script(root, task), "本次最终文案")
            task["brief"]["final_script"]["relative_path"] = "../../outside.txt"
            self.assertIsNone(_read_task_script(root, task))


if __name__ == "__main__":
    unittest.main()
