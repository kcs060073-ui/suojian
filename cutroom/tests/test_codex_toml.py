import tomllib
import unittest

from scripts.codex_toml import edit_cutroom


class CodexTomlTests(unittest.TestCase):
    def test_literal_header_examples_never_become_table_boundaries(self):
        for literal in ('"""\n[mcp_servers.cutroom]\ntext\n"""',
                        "'''\n[mcp_servers.cutroom]\ntext\n'''",
                        '"""ends with quotes"""""',
                        '"escaped \\" [mcp_servers.cutroom]"'):
            with self.subTest(literal=literal):
                before = 'notes=' + literal + '\n[mcp_servers.other]\ncommand="safe"\n'
                self.assertEqual(edit_cutroom(before), before)

    def test_unrelated_bytes_survive_target_removal(self):
        prefix = '# prefix\r\nscore=nan\r\nvalues = [\r\n ["value"], # array\r\n]\r\n'
        suffix = ('''[[skills.config]]
path='技能/说明'
enabled=false
[mcp_servers.'cutroom.extra']
command='untouched'
''')
        for header in ('[ mcp_servers . "cutroom" ] # target',
                       '["mcp_servers".\'cutroom\'.env]',
                       '[[mcp_servers.cutroom]]'):
            with self.subTest(header=header):
                self.assertEqual(edit_cutroom(prefix + header + '\nX="old"\n' + suffix), prefix + suffix)

    def test_install_and_reinstall_preserve_settings(self):
        source = '[mcp_servers.other]\ncommand="other"\n'
        block = '[mcp_servers.cutroom]\ncommand="python"\nargs=["--client","codex"]\n'
        first = edit_cutroom(source, block)
        second = edit_cutroom(first, block)
        self.assertEqual(first, second)
        self.assertEqual(tomllib.loads(second)['mcp_servers']['other'], {'command': 'other'})
        self.assertEqual(edit_cutroom(second), source + '\n')

    def test_inline_target_and_dotted_target_are_rejected_without_partial_edits(self):
        for source in ('mcp_servers.cutroom.command="python"\n',
                       'mcp_servers={cutroom={command="python"}}\n',
                       '[mcp_servers]\ncutroom.command="python"\n'):
            with self.subTest(source=source), self.assertRaises(ValueError):
                edit_cutroom(source)
