import tempfile,unittest
from pathlib import Path
from workbench.production import Store
from workbench.storyboard_animatic import render

class AnimaticGateTests(unittest.TestCase):
 def test_unconfirmed_boards_cannot_render(self):
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory)
   task=Store(root).create({'title':'预演测试','script':'口播','materials':[],'format':'portrait','workflowMode':'stick-narrative'})
   with self.assertRaises(ValueError):render(root,task['id'],'02')
   self.assertFalse((root/'outputs/storyboard-animatics').exists())

if __name__=='__main__':unittest.main()
