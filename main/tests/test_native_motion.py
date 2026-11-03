import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from suo_jian.analyzer.camera_motion import detect_camera_motion
from suo_jian.analyzer.motion_planning import scan_folder, plan_motion_shots


class NativeMotionTests(unittest.TestCase):
    def test_real_video_measurements_and_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            rng = np.random.default_rng(42)
            texture = rng.integers(0, 256, (240, 440, 3), dtype=np.uint8)
            for name, moving in [('static', False), ('pan', True)]:
                writer = cv2.VideoWriter(str(folder / f'{name}.mp4'), cv2.VideoWriter_fourcc(*'mp4v'), 30, (320, 240))
                self.assertTrue(writer.isOpened())
                for i in range(90):
                    offset = i if moving else 0
                    writer.write(texture[:, offset:offset+320])
                writer.release()
            self.assertEqual(detect_camera_motion(folder/'static.mp4')['type'], 'static')
            self.assertEqual(detect_camera_motion(folder/'pan.mp4')['type'], 'pan_right')
            index = scan_folder(folder)
            plan = plan_motion_shots([{'id': 'one', 'duration': 2, 'motion': 'pan_right'}], index, ratio='16:9')
            self.assertEqual(plan['clips'][0]['match'], 'exact')
            self.assertEqual(plan['clips'][0]['shot_id'], 'one')
            self.assertEqual(plan['engine'], 'suojian-local')
            self.assertFalse(plan['visual_accepted'])
            from flask import Flask
            from suo_jian.ui.native_motion import register_native_motion
            app = Flask(__name__)
            register_native_motion(app)
            client = app.test_client()
            response = client.post('/api/motion/plan', json={'folder': str(folder), 'ratio': '16:9',
                                   'shots': [{'duration': 2, 'motion': 'pan_right'}]})
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json['requires_cutroom'])
            self.assertEqual(response.json['plan']['clips'][0]['match'], 'exact')
            self.assertEqual(client.post('/api/motion/plan', json={'folder': str(folder)},
                             headers={'Origin': 'https://example.com'}).status_code, 403)
            with self.assertRaises(ValueError):
                plan_motion_shots([{'duration': 10}], index)
            with self.assertRaises(ValueError):
                detect_camera_motion(folder/'static.mp4', 2, 1)


if __name__ == '__main__':
    unittest.main()
