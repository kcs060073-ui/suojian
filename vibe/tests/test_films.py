import json
from pathlib import Path
import tempfile
import unittest
from workbench.films import artifact, catalog, as_jobs, register_film


class FilmCatalogTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'outputs').mkdir()

    def test_existing_delivery_and_related_assets_appear_without_registration(self):
        stem=self.root/'outputs/嘉定成片'
        video=stem.with_suffix('.mp4');video.write_bytes(b'video')
        for suffix in ('-封面.jpg','-制作工程.zip','.srt','-说明.md'):
            Path(str(stem)+suffix).write_bytes(b'asset')
        Path(str(stem)+'-制作回执.json').write_text(json.dumps({'title':'嘉定影片','durationSeconds':37.4,'resolution':[1080,1920],'mode':'混合模式','secret':'never expose'}),encoding='utf-8')
        records=catalog(self.root)
        self.assertEqual(len(records),1);film=records[0]
        self.assertEqual(film['title'],'嘉定影片');self.assertEqual(film['durationSeconds'],37.4)
        self.assertEqual(film['resolution'],[1080,1920]);self.assertEqual(len(film['artifacts']),5)
        self.assertTrue(film['poster']['url'].endswith('.jpg'));self.assertIn('%E5',film['video']['url'])
        self.assertNotIn('secret',json.dumps(film));self.assertEqual(film['editingMode'],'finished-cut')
        self.assertEqual(as_jobs(self.root)[0]['filmId'],film['id'])

    def test_future_nested_render_is_persisted_and_discovered(self):
        video=self.root/'outputs/runs/job/film.mp4';video.parent.mkdir(parents=True);video.write_bytes(b'video')
        self.assertEqual(catalog(self.root),[])
        register_film(self.root,video,{'name':'新片','format':'portrait','shots':[{'duration':150},{'duration':90,'enabled':False}]})
        film=catalog(self.root)[0]
        self.assertEqual(film['title'],'新片');self.assertEqual(film['durationSeconds'],5)
        self.assertEqual(film['resolution'],[1080,1920])
        self.assertEqual(len(catalog(self.root)),1)

    def test_registry_cannot_expose_outside_output_files(self):
        outside=self.root/'private.mp4';outside.write_bytes(b'private')
        self.assertIsNone(artifact(self.root,outside))
        with self.assertRaises(ValueError):register_film(self.root,outside)
        path=self.root/'work/films/registry.json';path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'private.mp4':{}}),encoding='utf-8')
        self.assertEqual(catalog(self.root),[])

    def test_malformed_receipt_and_registry_do_not_hide_finished_video(self):
        video=self.root/'outputs/finished.mp4';video.write_bytes(b'video')
        video.with_name('finished-制作回执.json').write_text('[]',encoding='utf-8')
        path=self.root/'work/films/registry.json';path.parent.mkdir(parents=True);path.write_text('[]',encoding='utf-8')
        self.assertEqual(catalog(self.root)[0]['title'],'finished')
