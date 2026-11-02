import unittest,tempfile,shutil
from pathlib import Path
from media_catalog.emphasis_packaging import make_emphasis,split_emphasis_cues

class EmphasisTests(unittest.TestCase):
    def test_semantic_split_preserves_copy_and_phrase_window(self):
        rows=[{'text':'层高11到12米，一层承重5吨，','start':0,'end':4,'dur':4,'i':1}]
        result=split_emphasis_cues(rows)
        self.assertEqual(''.join(x['text'] for x in result),rows[0]['text'])
        self.assertEqual(len(result),2)
        self.assertEqual(result[0]['start'],0)
        self.assertEqual(result[-1]['end'],4)
        self.assertEqual(result[0]['end'],result[1]['start'])

    def test_attribute_and_number_replace_body_and_keep_reference_positions(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);fonts=root/'assets/fonts';fonts.mkdir(parents=True)
            existing=Path(__file__).resolve().parents[1]/'assets/fonts'
            for name in ['5 Alimama ShuHeiTi 阿里妈妈数黑.otf','SmileySans-Oblique.otf']:shutil.copyfile(existing/name,fonts/name)
            events=[{'displayText':text,'start':i,'end':i+1} for i,text in enumerate(['国资','可租可售','层高11到12米','普通字幕','还是换层高11米'])]
            policy={'width':960,'height':540,'subtitle':{'primary_hex':'#FFE820'},'emphasis':{'enabled':True,'attribute_center_y':.9,'number_center_y':.54,'attribute_template':'business-label-A','number_template':'slanted-number-B'}}
            overlays,report=make_emphasis(root,'task-test','plan-test',events,policy)
            self.assertEqual(len(overlays),3)
            self.assertTrue(all(Path(x['src']).is_file() for x in overlays))
            self.assertEqual(events[3]['displayText'],'普通字幕')
            self.assertEqual(events[4]['displayText'],'还是换层高11米')
            self.assertTrue(all(events[i]['displayText']=='' for i in range(3)))
            self.assertEqual(report[0]['center_y'],.9)
            self.assertEqual(report[2]['center_y'],.54)

if __name__=='__main__':unittest.main()
