from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).with_name('pii_scan.py')

class PiiScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)/'export'
        self.root.mkdir()
        self.allow = Path(self.temp.name)/'allow'
        self.deny = Path(self.temp.name)/'deny'
        self.allow.write_text('public-handle\n')
        self.deny.write_text('obsolete-handle\n')
    def tearDown(self):
        self.temp.cleanup()
    def run_scan(self, *extra):
        result = subprocess.run([sys.executable, str(SCRIPT), str(self.root),
            '--allow-file', str(self.allow), '--deny-file', str(self.deny), *extra],
            capture_output=True, text=True)
        self.assertNotIn('obsolete-handle', result.stdout + result.stderr)
        return result, json.loads(result.stdout)
    def test_approved_holder_but_denied_identity_on_same_line(self):
        (self.root/'LICENSE').write_text('Copyright (c) 2026 public-handle\n')
        result, report = self.run_scan()
        self.assertEqual(result.returncode, 0)
        (self.root/'README').write_text('https://github.com/public-handle is also obsolete-handle\n')
        result, report = self.run_scan()
        self.assertEqual(result.returncode, 1)
        self.assertIn('denied-identity', {x['category'] for x in report['findings']})
    def test_paths_and_names_redacted(self):
        self.allow.write_text('')
        (self.root/'obsolete-handle.txt').write_text('author: Example User\nemail: user@example.com\n/home/public-handle/file\n+1 202 555 0100\n')
        result, report = self.run_scan()
        for value in ('Example User', 'user@example.com', 'public-handle', '+1 202 555 0100'):
            self.assertNotIn(value, result.stdout + result.stderr)
        self.assertTrue(all(x.get('path_redacted') for x in report['findings']))
        self.assertEqual({x['category'] for x in report['findings']},
            {'filename-identity-candidate', 'author-field',
             'email', 'home-identity', 'phone-candidate'})
        self.assertEqual(result.returncode, 1)
    def test_denied_value_crosses_chunk_boundary(self):
        (self.root/'data').write_bytes(b'x' * 65530 + b'obsolete-handle')
        result, report = self.run_scan()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(report['findings']), 1)
    def test_structured_authors(self):
        for text, identity in (
            ('"author": "Example User"', 'Example User'),
            ("'maintainer': 'Example User'", 'Example User'),
            ('"author": {"name": "Example User"}', 'Example User'),
            ('authors = ["Example User <user@example.com>"]', 'Example User <user@example.com>'),
            ('author: Example User', 'Example User'),
            ('metadata = {"creator": "Example User"}', 'Example User'),
        ):
            with self.subTest(text=text):
                (self.root/'metadata').write_text(text + '\n')
                result, report = self.run_scan()
                for value in (identity, 'Example User', 'user@example.com'):
                    self.assertNotIn(value, result.stdout + result.stderr)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(sum(x['category']=='author-field' for x in report['findings']), 1)
    def test_author_policy_uses_extracted_literal(self):
        (self.root/'metadata').write_text('"author": {"name": "public-handle"}\nauthor = "public-handle"\n')
        result, report = self.run_scan()
        self.assertNotIn('public-handle', result.stdout + result.stderr)
        self.assertEqual(result.returncode, 0)
        self.deny.write_text('public-handle\n')
        result, report = self.run_scan()
        self.assertNotIn('public-handle', result.stdout + result.stderr)
        self.assertEqual({x['category'] for x in report['findings']}, {'denied-identity'})
        self.assertEqual(result.returncode, 1)
    def test_author_function_calls_are_not_fields(self):
        (self.root/'code').write_text('author = get_author()\n<author>Example User</author>\n')
        result, report = self.run_scan()
        for value in ('get_author()', 'Example User'):
            self.assertNotIn(value, result.stdout + result.stderr)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(report['findings'], [])
    def test_ssh_remotes_do_not_report_git_email(self):
        self.allow.write_text('')
        for text in ('git@github.com:public-handle/repo.git',
                     'GIT@codeberg.org:public-handle/repo.git'):
            with self.subTest(text=text):
                (self.root/'remote').write_text(text)
                result, report = self.run_scan()
                for value in (text, 'public-handle', 'git@github.com', 'GIT@codeberg.org'):
                    self.assertNotIn(value, result.stdout + result.stderr)
                self.assertEqual(result.returncode, 1)
                self.assertEqual([x['category'] for x in report['findings']], ['git-ssh-remote'])
    def test_git_email_without_remote_is_reported(self):
        (self.root/'contact').write_text('git@example.com')
        result, report = self.run_scan()
        self.assertNotIn('git@example.com', result.stdout + result.stderr)
        self.assertEqual(result.returncode, 1)
        self.assertEqual([x['category'] for x in report['findings']], ['email'])
    def test_profile_prefixes(self):
        self.allow.write_text('')
        for prefix in ('https://gitlab.com/', 'https://bitbucket.org/',
                       'https://codeberg.org/', 'https://x.com/',
                       'https://twitter.com/', 'https://linkedin.com/in/',
                       'https://medium.com/@'):
            with self.subTest(prefix=prefix):
                (self.root/'profile').write_text(prefix + 'public-handle')
                result, report = self.run_scan()
                self.assertNotIn('public-handle', result.stdout + result.stderr)
                self.assertEqual(result.returncode, 1)
                self.assertEqual([x['category'] for x in report['findings']], ['profile-link'])
        (self.root/'profile').write_text('https://gitlab.com/explore')
        result, report = self.run_scan()
        self.assertNotIn('https://gitlab.com/explore', result.stdout + result.stderr)
        self.assertEqual(result.returncode, 1)
        self.assertEqual([x['category'] for x in report['findings']], ['profile-link'])
    def test_ssh_and_profile_policy_precedence(self):
        (self.root/'links').write_text('git@github.com:public-handle/repo.git\n'
            'https://x.com/public-handle\n')
        result, report = self.run_scan()
        self.assertNotIn('public-handle', result.stdout + result.stderr)
        self.assertEqual(result.returncode, 0)
        self.deny.write_text('public-handle\n')
        result, report = self.run_scan()
        self.assertNotIn('public-handle', result.stdout + result.stderr)
        self.assertEqual(result.returncode, 1)
        self.assertEqual([x['category'] for x in report['findings']], ['denied-identity'] * 2)
    def test_image_metadata_signatures_repeat_and_ignore_allow_file(self):
        for signature in (b'Exif\x00\x00', b'<x:xmpmeta'):
            with self.subTest(signature=signature):
                self.allow.write_bytes(signature + b'\n')
                (self.root/'image').write_bytes(b'\x00' + b'x' * 65532 + signature +
                    b'Example User\n' + b'x' * 65536 + signature)
                result, report = self.run_scan()
                for value in (signature.decode(), 'Example User'):
                    self.assertNotIn(value, result.stdout + result.stderr)
                self.assertEqual(result.returncode, 1)
                self.assertEqual([x['category'] for x in report['findings']], ['image-metadata'] * 2)
                self.assertTrue(all(x['content_kind'] == 'binary' for x in report['findings']))
    def test_dropped_image_signatures_are_ignored(self):
        (self.root/'image').write_bytes(b'\x00iTXt\neXIf\nhttp://ns.adobe.com/xap/1.0/\nGPSInfo\nGPSLatitude')
        result, report = self.run_scan()
        for value in ('iTXt', 'eXIf', 'http://ns.adobe.com/xap/1.0/', 'GPSInfo', 'GPSLatitude'):
            self.assertNotIn(value, result.stdout + result.stderr)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(report['findings'], [])
    def test_history_and_commit_identity(self):
        def git(*args):
            subprocess.run(['git', *args], cwd=self.root, check=True, capture_output=True)
        git('init', '-q')
        git('config', 'user.name', 'public-handle')
        git('config', 'user.email', 'public@example.org')
        self.allow.write_text('public-handle\npublic@example.org\n')
        (self.root/'LICENSE').write_text('Copyright (c) 2026 obsolete-handle\n')
        git('add', 'LICENSE'); git('commit', '-qm', 'initial')
        (self.root/'LICENSE').write_text('Copyright (c) 2026 public-handle\n')
        git('commit', '-qam', 'fix')
        result, report = self.run_scan('--history')
        self.assertEqual(result.returncode, 1)
        self.assertTrue(any(x['scope']=='history' for x in report['findings']))
        self.assertFalse(any(x['scope']=='commit-metadata' for x in report['findings']))
        git('commit', '--allow-empty', '-qm', 'obsolete-handle')
        result, report = self.run_scan('--history')
        self.assertTrue(any(x['scope']=='commit-metadata' for x in report['findings']))
    def test_unreadable_policy_returns_redacted_error(self):
        self.deny.unlink()
        result, report = self.run_scan()
        self.assertEqual(result.returncode, 2)
        self.assertFalse(report['summary']['complete'])

if __name__ == '__main__':
    unittest.main()
