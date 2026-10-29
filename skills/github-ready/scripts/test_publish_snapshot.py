from pathlib import Path
import json
import os
import signal
import shlex
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).with_name('publish_snapshot.sh').resolve()
IDENTITY = 'Example User <user@example.com>'
PLANTED = 'ghp_' + 'Q7' * 20
GATE = '''import json
import os
import subprocess
import sys

with open(os.environ['GATE_LOG'], 'w') as target:
    json.dump(sys.argv[1:], target)
if os.environ.get('RAW_OUTPUT'):
    print(os.environ['PLANTED'])
else:  # Valid gate JSON; the planted value sits in a field the summary never prints.
    checks = [{'name': 'pii_scan', 'status': 'pass', 'categories': {}}]
    if os.environ.get('WITH_OVERLAP'):
        checks.append({'name': 'value_overlap_scan', 'status': 'pass', 'categories': {}})
    print(json.dumps({'checks': checks, 'limitations': [os.environ['PLANTED']]}))
print(os.environ['PLANTED'], file=sys.stderr)
if os.environ.get('MOVE_BRANCH'):
    subprocess.check_call(['git', '-C', os.environ['SOURCE_REPO'], 'update-ref',
        'refs/heads/publish', os.environ['MOVE_BRANCH']])
if os.environ.get('SIGNAL_PARENT'):
    import signal
    os.kill(os.getppid(), getattr(signal, os.environ['SIGNAL_PARENT']))
sys.exit(int(os.environ.get('GATE_EXIT', '0')))
'''


@unittest.skipUnless(shutil.which('bash') and shutil.which('git'), 'bash and git required')
class PublishSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root/'source'
        self.repo.mkdir()
        self.out = self.root/'output clone'
        self.gate = self.root/'gate.py'
        self.gate.write_text(GATE)
        self.log = self.root/'gate.json'
        self.env = {key: value for key, value in os.environ.items()
            if not key.startswith('GIT_') and key not in ('BASH_ENV', 'ENV', 'PUBLISH_GATE')}
        self.env.update({'HOME': str(self.root),
            'TMPDIR': str(self.root), 'LC_ALL': 'C', 'GIT_CONFIG_NOSYSTEM': '1',
            'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_TERMINAL_PROMPT': '0',
            'GIT_AUTHOR_DATE': '2020-01-02T03:04:05+00:00',
            'GIT_COMMITTER_DATE': '2020-01-02T03:04:05+00:00',
            'PUBLISH_GATE': str(self.gate), 'GATE_LOG': str(self.log),
            'PLANTED': PLANTED, 'SOURCE_REPO': str(self.repo)})
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'public-handle')
        self.git('config', 'user.email', 'source@example.com')
        (self.repo/'a.txt').write_text('first\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'first')
        (self.repo/'a.txt').write_text('second\n')
        (self.repo/'sub').mkdir()
        (self.repo/'sub'/'b.txt').write_text(PLANTED + '\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'second')
        self.git('tag', 'private-tag')

    def git(self, *args, repo=None):
        return subprocess.run(['git', '-C', str(repo or self.repo), *args],
            env=self.env, capture_output=True, text=True, check=True).stdout.strip()

    def publish(self, *args, env=None, cwd=None, bash=None, script=SCRIPT):
        result = subprocess.run([bash or 'bash', str(script), '--identity', IDENTITY,
            '--out', str(self.out), *args], cwd=cwd or self.repo,
            env={**self.env, **(env or {})}, capture_output=True, text=True)
        for value in (PLANTED, IDENTITY, 'user@example.com', 'source@example.com', 'public-handle'):
            self.assertNotIn(value, result.stdout + result.stderr)
        return result

    def success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def refused(self, result, message):
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(message, result.stderr)
        self.no_branch()
        self.assertFalse(self.out.exists())

    def tip(self):
        return self.git('rev-parse', 'refs/heads/publish')

    def no_branch(self):
        self.assertEqual(self.git('for-each-ref', '--format=%(refname)', 'refs/heads/publish'), '')

    def state(self):
        refs = self.git('for-each-ref', '--format=%(refname) %(objectname)').splitlines()
        return (self.git('symbolic-ref', 'HEAD'), self.git('rev-parse', 'HEAD'),
            self.git('ls-files', '-s'), self.git('status', '--porcelain'),
            '\n'.join(line for line in refs if not line.startswith('refs/heads/publish ')))

    def change_head(self):
        (self.repo/'a.txt').write_text('third\n')
        self.git('commit', '-qam', 'third')

    def test_parentless_tree_identity_and_isolation(self):
        (self.repo/'untracked').write_text(PLANTED)
        before = self.state()
        result = self.publish('--', '--allow-file', 'allow', '--deny-file', 'deny', '--source', 'source')
        self.success(result)
        tip = self.tip()
        self.assertEqual(self.git('rev-list', '--parents', 'publish'), tip)
        tree = self.git('rev-parse', 'HEAD^{tree}')
        self.assertEqual(self.git('rev-parse', 'publish^{tree}'), tree)
        self.assertEqual(self.git('show', '-s', '--format=%an <%ae>|%cn <%ce>', 'publish'),
            IDENTITY + '|' + IDENTITY)
        self.assertEqual(self.git('show', '-s', '--format=%cd', '--date=raw', 'publish'),
            self.git('show', '-s', '--format=%cd', '--date=raw', 'HEAD'))
        self.assertEqual(self.git('show', '-s', '--format=%s', 'publish'), 'Initial import')
        self.assertNotIn('untracked', self.git('ls-tree', '-r', '--name-only', 'publish').splitlines())
        self.assertEqual(self.git('rev-list', '--all', '--count', repo=self.out), '1')
        self.assertEqual(self.git('remote', repo=self.out), '')
        self.assertEqual(self.git('for-each-ref', '--format=%(refname)', repo=self.out), 'refs/heads/publish')
        private = subprocess.run(['git', '-C', str(self.out), 'cat-file', '-e',
            self.git('rev-parse', 'HEAD')], env=self.env, capture_output=True)
        self.assertNotEqual(private.returncode, 0)
        self.assertEqual(self.git('config', '--get', 'publish-snapshot.source', repo=self.out), str(self.repo))
        log = json.loads(self.log.read_text())
        self.assertEqual(log, [str(self.out), '--identity', IDENTITY, '--max-commits', '1',
            '--allow-file', 'allow', '--deny-file', 'deny', '--source', 'source'])
        push = next(line for line in result.stdout.splitlines() if line.startswith('git -C '))
        self.assertEqual(shlex.split(push), ['git', '-C', str(self.out), 'push',
            '--force-with-lease=main:', '<public-remote-url>', 'publish:main'])
        self.assertIn('git ls-remote ', result.stdout)
        self.assertIn('must print ' + tip, result.stdout)
        for item in ('commit: ' + tip, 'tree: ' + tree, 'files: 2', 'out: ' + str(self.out)):
            self.assertIn(item, result.stdout)
        self.assertEqual(before, self.state())

    def test_dirty_tracked_files_refused(self):
        for staged in (False, True):
            with self.subTest(staged=staged):
                (self.repo/'a.txt').write_text(PLANTED + '\n')
                if staged:
                    self.git('add', 'a.txt')
                result = self.publish()
                self.refused(result, 'uncommitted changes')

    def test_gate_status_and_accept_review(self):
        before = self.state()
        for code, args, status in ((1, (), 'REVIEW'), (2, (), 'FAIL'),
                (2, ('--accept-review',), 'FAIL')):
            with self.subTest(code=code, args=args):
                result = self.publish(*args, env={'GATE_EXIT': str(code)})
                self.refused(result, 'gate: ' + status)
                self.assertNotIn(' push ', result.stdout)
                self.assertEqual(before, self.state())
        result = self.publish('--accept-review', env={'GATE_EXIT': '1'})
        self.success(result)
        self.assertIn('REVIEW accepted', result.stdout)

    def test_existing_history_branch_refused_and_untouched(self):
        self.git('branch', 'publish', 'main')
        before = self.state(), self.tip()
        result = self.publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not a parentless snapshot', result.stderr)
        self.assertEqual(before, (self.state(), self.tip()))
        self.assertFalse(self.log.exists())

    def test_previous_snapshot_replaced_with_lease(self):
        self.success(self.publish())
        previous = self.tip()
        (self.out/'leftover').write_text('untracked\n')
        self.success(self.publish())
        self.assertEqual(self.tip(), previous)
        self.assertFalse((self.out/'leftover').exists())
        self.change_head()
        before = self.state()
        result = self.publish('--remote-url', 'https://example.com/public.git')
        self.success(result)
        self.assertNotEqual(self.tip(), previous)
        self.assertEqual(self.git('rev-list', '--count', 'publish'), '1')
        self.assertIn('--force-with-lease=main:' + previous, result.stdout)
        self.assertIn('https://example.com/public.git', result.stdout)
        self.assertEqual(before, self.state())

    def test_failure_and_signals_restore_previous_tip(self):
        self.success(self.publish())
        previous = self.tip()
        self.change_head()
        before = self.state()
        cases = [({'GATE_EXIT': '2'}, 1)] + [
            ({'SIGNAL_PARENT': name}, 128 + getattr(signal, name))
            for name in ('SIGINT', 'SIGTERM')]
        for env, code in cases:
            with self.subTest(env=env):
                self.assertEqual(self.publish(env=env).returncode, code)
                self.assertEqual(self.tip(), previous)
                self.assertFalse(self.out.exists())
                self.assertEqual(before, self.state())

    def test_excludes_leave_source_index_and_worktree_unchanged(self):
        exclude = self.root/'excludes'
        (self.repo/'[one].txt').write_text('exclude\n')
        (self.repo/'o.txt').write_text('retain\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'literal paths')
        exclude.write_bytes(b'# comment\r\n\r\nsub/\r\nmissing\n[one].txt\r\n')
        before = self.state()
        index = Path(self.git('rev-parse', '--absolute-git-dir'))/'index'
        index_before = index.read_bytes()
        content_before = (self.repo/'sub'/'b.txt').read_bytes()
        self.success(self.publish('--exclude-file', str(exclude)))
        self.assertEqual(self.git('ls-tree', '-r', '--name-only', 'publish'), 'a.txt\no.txt')
        self.assertEqual(index_before, index.read_bytes())
        self.assertEqual(content_before, (self.repo/'sub'/'b.txt').read_bytes())
        self.assertEqual(before, self.state())

    def test_unreadable_gate_output_is_refused(self):
        before = self.state()
        result = self.publish(env={'RAW_OUTPUT': '1'})
        self.refused(result, 'gate output unreadable')
        self.assertNotIn(' push ', result.stdout)
        self.assertFalse(self.out.exists())
        self.assertEqual(before, self.state())

    def test_value_overlap_skip_is_reported(self):
        result = self.publish()
        self.success(result)
        self.assertIn('value-overlap: skipped', result.stdout)
        result = self.publish(env={'WITH_OVERLAP': '1'})
        self.success(result)
        self.assertNotIn('value-overlap: skipped', result.stdout)

    def test_existing_unmarked_out_untouched(self):
        self.out.mkdir()
        sentinel = self.out/'keep'
        sentinel.write_text(PLANTED)
        result = self.publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sentinel.read_text(), PLANTED)
        self.no_branch()

    def test_cleanup_preserves_concurrently_changed_branch(self):
        main = self.git('rev-parse', 'main')
        result = self.publish(env={'GATE_EXIT': '2', 'MOVE_BRANCH': main})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.tip(), main)
        self.assertFalse(self.out.exists())

    def test_checked_out_branches_refused(self):
        self.git('branch', 'publish')
        worktree = self.root/'other-worktree'
        self.git('worktree', 'add', '-q', str(worktree), 'publish')
        before = self.state(), self.tip()
        for branch in ('main', 'publish'):
            with self.subTest(branch=branch):
                result = self.publish('--branch', branch)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('checked out', result.stderr)
                self.assertEqual(before, (self.state(), self.tip()))

    def test_invalid_branch_and_identity_refused(self):
        cases = [('--branch', '../invalid'), ('--branch', '-invalid'),
            ('--branch', 'bad name'), ('--identity', 'user@example.com'),
            ('--identity', 'Example User <invalid>'),
            ('--identity', 'Example User <invalid@>'),
            ('--identity', 'Example User\n <user@example.com>')]
        for args in cases:
            with self.subTest(args=args):
                result = self.publish(*args)
                self.refused(result, 'invalid branch' if args[0] == '--branch' else 'identity')

    def test_usage_and_help_exit_codes(self):
        for args in (('--branch',), ('--unknown',), ('--chain',), ('--identity', ''), ('--out', '')):
            with self.subTest(args=args):
                result = self.publish(*args)
                self.assertEqual(result.returncode, 2)
                self.assertIn('usage:', result.stderr)
                self.no_branch()
        result = self.publish('--help')
        self.assertEqual(result.returncode, 0)
        self.assertIn('usage:', result.stdout)
        self.no_branch()

    def test_identity_is_verified_before_ref_update(self):
        bin_dir = self.root/'bin'
        bin_dir.mkdir()
        self.env['PATH'] = str(bin_dir) + os.pathsep + self.env['PATH']
        git = bin_dir/'git'
        git.write_text('''#!/usr/bin/env python3
import os
import sys

if 'commit-tree' in sys.argv:
    os.environ['GIT_COMMITTER_NAME'] = 'public-handle'
os.execv(os.environ['REAL_GIT'], ['git', *sys.argv[1:]])
''')
        git.chmod(0o755)
        self.env['REAL_GIT'] = shutil.which('git')
        result = self.publish()
        self.refused(result, 'identity verification failed')

    def test_missing_gate_refused_before_ref_update(self):
        result = self.publish(env={'PUBLISH_GATE': str(self.root/'missing-gate')})
        self.refused(result, 'set PUBLISH_GATE')

    def test_missing_head_refused(self):
        self.repo = self.root/'empty'
        self.repo.mkdir()
        self.git('init', '-q', '-b', 'main')
        result = self.publish()
        self.refused(result, 'HEAD has no commit')

    def test_out_ancestor_and_symlink_inside_source_refused(self):
        link = self.root/'link'
        link.symlink_to(self.repo, target_is_directory=True)
        for out in (self.repo/'output', self.root, link/'output', self.repo/'.git'/'output'):
            with self.subTest(out=out):
                self.out = out
                result = self.publish()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('unsafe output', result.stderr)
                self.assertTrue((self.repo/'a.txt').exists())
                self.no_branch()

    def test_invalid_exclusion_leaves_source_and_owned_clone_untouched(self):
        self.success(self.publish())
        before = self.state(), self.tip()
        exclude = self.root/'excludes'
        for value in ('../a.txt\n', '/a.txt\n', '.\n'):
            with self.subTest(value=value):
                exclude.write_text(value)
                result = self.publish('--exclude-file', str(exclude))
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(before, (self.state(), self.tip()))
                self.assertTrue(self.out.is_dir())

    def test_inherited_git_overrides_are_unset(self):
        wrong = str(self.root/'nonexistent')
        self.success(self.publish(env={'GIT_DIR': wrong, 'GIT_WORK_TREE': wrong,
            'GIT_INDEX_FILE': wrong, 'GIT_COMMON_DIR': wrong}))

    def test_runs_from_subdirectory_and_default_gate_is_beside_script(self):
        scripts = self.root/'tools'
        scripts.mkdir()
        copy = scripts/'publish_snapshot.sh'
        shutil.copy2(SCRIPT, copy)
        (scripts/'publish_gate.py').write_text(GATE)
        self.success(self.publish(script=copy, cwd=self.repo/'sub', env={'PUBLISH_GATE': ''}))

    @unittest.skipUnless(Path('/bin/bash').exists(), '/bin/bash unavailable')
    def test_system_bash_compatibility(self):
        exclude = self.root/'excludes'
        exclude.write_text('  # comment\n\n')
        self.success(self.publish('--exclude-file', str(exclude), bash='/bin/bash'))
        self.assertEqual(self.git('rev-parse', 'publish^{tree}'), self.git('rev-parse', 'HEAD^{tree}'))
        exclude.write_text('sub\n')
        result = self.publish('--branch', 'snapshot', '--exclude-file', str(exclude), bash='/bin/bash')
        self.success(result)
        self.assertIn('snapshot:main', result.stdout)
        self.assertEqual(self.git('rev-list', '--count', 'snapshot'), '1')


if __name__ == '__main__':
    unittest.main()
