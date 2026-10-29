from pathlib import Path
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parent
IDENTITY = 'public-handle <123+public-handle@users.noreply.github.com>'
TOKEN = 'ghp_' + 'A' * 28
PRIVATE_AUTHOR = 'Private Person'
PRIVATE_EMAIL = 'private@example.org'
PRIVATE_NUMBER = '287413.65'
# Fails unless the clone is isolated: no inherited GIT_DIR and no origin remote.
TEST_COMMAND = ('python3 -c "import os, pathlib, subprocess; '
                'assert pathlib.Path(\'README.md\').exists(); '
                'assert \'GIT_DIR\' not in os.environ; '
                'assert not subprocess.run([\'git\', \'remote\'], capture_output=True, check=True).stdout"')


@unittest.skipUnless(shutil.which('bash') and shutil.which('git'), 'bash and git required')
class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root/'source'
        self.out = self.root/'output'
        self.private = self.root/'private'
        for directory in (self.repo, self.private):
            directory.mkdir()
        self.env = {key: value for key, value in os.environ.items()
            if not key.startswith('GIT_') and key not in ('BASH_ENV', 'ENV', 'PUBLISH_GATE')}
        self.env.update({'HOME': str(self.root), 'TMPDIR': str(self.root),
            'LC_ALL': 'C', 'GIT_CONFIG_NOSYSTEM': '1',
            'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_TERMINAL_PROMPT': '0',
            'GIT_AUTHOR_DATE': '2020-01-02T03:04:05+00:00',
            'GIT_COMMITTER_DATE': '2020-01-02T03:04:05+00:00'})
        self.source = self.private/'values.json'
        self.source.write_text(json.dumps({'amount': PRIVATE_NUMBER}), encoding='utf-8')
        self.allow = self.root/'allow.txt'
        self.allow.write_text('public-handle\n123+public-handle@users.noreply.github.com\n',
            encoding='utf-8')
        self.deny = self.root/'deny.txt'
        self.deny.write_text(PRIVATE_AUTHOR + '\n', encoding='utf-8')
        self.exclude = self.root/'excludes.txt'
        self.exclude.write_text('notes/private-plan.md\n', encoding='utf-8')
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Example User')
        self.git('config', 'user.email', PRIVATE_EMAIL)
        (self.repo/'README.md').write_text('Public example\n', encoding='utf-8')
        (self.repo/'token.txt').write_text(TOKEN + '\n', encoding='utf-8')
        self.package = self.repo/'package.json'
        self.package.write_text(json.dumps({'author': PRIVATE_AUTHOR}) + '\n', encoding='utf-8')
        self.git('add', '.')
        self.git('commit', '-qm', 'initial fixture')
        old_commit = self.git('rev-parse', 'HEAD').stdout.strip()
        self.old_blobs = [self.git('rev-parse', old_commit + ':' + path).stdout.strip()
            for path in ('token.txt', 'package.json')]
        (self.repo/'token.txt').unlink()
        self.package.write_text(json.dumps({'author': 'public-handle'}) + '\n', encoding='utf-8')
        (self.repo/'notes').mkdir()
        (self.repo/'notes'/'private-plan.md').write_text('Internal plan\n', encoding='utf-8')
        self.example = self.repo/'example.txt'
        self.example.write_text('amount = ' + PRIVATE_NUMBER + '\n', encoding='utf-8')
        self.git('add', '-A')
        self.git('commit', '-qm', 'prepare public fixture')

    def run_command(self, args, cwd=None):
        result = subprocess.run(args, cwd=cwd or self.repo, env=self.env,
            capture_output=True, text=True)
        for value in (TOKEN, PRIVATE_AUTHOR, PRIVATE_EMAIL, PRIVATE_NUMBER):
            self.assertTrue(value not in result.stdout + result.stderr,
                'captured output leaked a planted private value')
        return result

    def git(self, *args, repo=None, check=True):
        result = self.run_command(['git', '-C', str(repo or self.repo), *args])
        if check:
            self.assertEqual(result.returncode, 0, 'fixture git command failed')
        return result

    def publish(self):
        return self.run_command(['bash', str(SCRIPTS/'publish_snapshot.sh'),
            '--identity', IDENTITY, '--out', str(self.out),
            '--exclude-file', str(self.exclude), '--', '--allow-file', str(self.allow),
            '--deny-file', str(self.deny), '--source', str(self.source)])

    def fresh_check(self, command):
        result = self.run_command([sys.executable, str(SCRIPTS/'fresh_clone_check.py'),
            str(self.out), '--run', command, '--watch', str(self.private)])
        return result, json.loads(result.stdout)

    def test_review_then_isolated_snapshot_and_fresh_clone(self):
        result = self.publish()
        self.assertEqual(result.returncode, 1,
            'publish_snapshot.sh: real gate must reject copied private values with REVIEW')
        self.assertIn('gate: REVIEW', result.stderr)
        self.assertIn('  value_overlap_scan: review {"value-overlap": 1}', result.stdout)
        for name in ('commit-count', 'refs', 'identities', 'safe_repo_scan', 'pii_scan'):
            self.assertIn('  ' + name + ': pass {}', result.stdout)
        self.assertEqual(self.git('for-each-ref', '--format=%(refname)',
            'refs/heads/publish').stdout, '',
            'publish_snapshot.sh: REVIEW must remove the new snapshot branch')
        self.assertFalse(self.out.exists(),
            'publish_snapshot.sh: REVIEW must remove the output clone')
        self.assertNotIn(' push ', result.stdout)

        self.example.write_text('amount = public-example\n', encoding='utf-8')
        self.git('add', 'example.txt')
        self.git('commit', '-qm', 'remove copied value')
        result = self.publish()
        self.assertEqual(result.returncode, 0,
            'publish_snapshot.sh/publish_gate.py: sanitized snapshot must pass the real gate; '
            + result.stdout + result.stderr)
        self.assertIn('gate: PASS', result.stdout)
        for name in ('commit-count', 'refs', 'identities', 'safe_repo_scan', 'pii_scan',
                'value_overlap_scan'):
            self.assertIn('  ' + name + ': pass {}', result.stdout)
        self.assertEqual(self.git('rev-list', '--all', '--count', repo=self.out).stdout.strip(), '1',
            'publish_snapshot.sh: output must contain exactly one reachable commit')
        self.assertEqual(self.git('show', '-s', '--format=%an <%ae>%n%cn <%ce>',
            'HEAD', repo=self.out).stdout.splitlines(), [IDENTITY, IDENTITY],
            'publish_snapshot.sh: author and committer must use the explicit identity')
        paths = self.git('ls-tree', '-r', '--name-only', 'HEAD', repo=self.out).stdout.splitlines()
        self.assertNotIn('notes/private-plan.md', paths,
            'publish_snapshot.sh: excluded file must be absent from the output tree')
        self.assertFalse((self.out/'notes'/'private-plan.md').exists())
        for oid in self.old_blobs:
            with self.subTest(blob_id=oid):
                self.assertNotEqual(self.git('cat-file', '-e', oid,
                    repo=self.out, check=False).returncode, 0,
                    'publish_snapshot.sh: private historical blob remains in the object database')
        self.assertEqual(self.git('remote', repo=self.out).stdout, '',
            'publish_snapshot.sh: output clone must have no remotes')
        push = [shlex.split(line) for line in result.stdout.splitlines()
            if line.startswith('git -C ')]
        self.assertEqual(len(push), 1)
        self.assertEqual(push[0][:5], ['git', '-C', str(self.out), 'push',
            '--force-with-lease=main:'],
            'publish_snapshot.sh: printed push command must use the output and an empty main lease')

        result, report = self.fresh_check(TEST_COMMAND)
        self.assertEqual(result.returncode, 0,
            'fresh_clone_check.py: README command must pass without changing the private directory')
        self.assertEqual(report['status'], 'pass')
        self.assertEqual(report['commands'][0]['exit_code'], 0)
        self.assertEqual(report['commit'], self.git('rev-parse', 'HEAD', repo=self.out).stdout.strip())
        self.assertFalse(report['watch'][0]['changed'])
        touched = self.private/'touched'
        command = 'python3 -c ' + shlex.quote('import pathlib; pathlib.Path(' + repr(str(touched)) + ').touch()')
        result, report = self.fresh_check(command)
        self.assertTrue(touched.exists(), 'watch fixture command must actually touch the private directory')
        self.assertEqual(result.returncode, 2,
            'fresh_clone_check.py: a successful command changing the watched directory must exit 2')
        self.assertEqual(report['status'], 'fail')
        self.assertEqual(report['commands'][0]['exit_code'], 0)
        self.assertTrue(report['watch'][0]['changed'])


if __name__ == '__main__':
    unittest.main()
