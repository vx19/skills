from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).with_name('value_overlap_scan.py')

class ValueOverlapScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.repo = base/'repo'
        self.repo.mkdir()
        if subprocess.run(['git', 'init', '-q', str(self.repo)], capture_output=True).returncode:
            self.skipTest('git init blocked')
        self.ledger = base/'ledger.json'
        self.ledger.write_text(json.dumps({'rows': [
            {'date': '2026-02-17', 'eur': '2750.00', 'inr': 287413.65, 'rate': 104.8762,
             'note': 'ref EUR3189.45@97.31'}]}))
        self.trades = base/'trades.csv'
        self.trades.write_text('isin,quantity,price\nXX0000000000,37.4219,58.91\n')
    def tearDown(self):
        self.temp.cleanup()
    def track(self, name, text):
        (self.repo/name).write_text(text)
        subprocess.run(['git', '-C', str(self.repo), 'add', name], check=True, capture_output=True)
    def run_scan(self, *extra):
        return self.scan('--source', str(self.ledger), '--source', str(self.trades),
                         '--csv-column', 'quantity', *extra)
    def assert_private(self, result):
        for value in ('287413.65', '287,413.65', '28741365', '104.8762', '3189.45', '37.4219'):
            self.assertNotIn(value, result.stdout + result.stderr)
    def scan(self, *args):
        result = subprocess.run([sys.executable, str(SCRIPT), str(self.repo), *args],
                                capture_output=True, text=True)
        self.assert_private(result)
        return result, json.loads(result.stdout or result.stderr)
    def test_finds_values_however_written_and_reports_lines_only(self):
        self.track('t.py', 'rate = 104.8762\nd = "EUR3189.45@x"\na = "287,413.65"\nq = "37.42190"\n')
        result, report = self.run_scan()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report['findings'], [{'path': 't.py', 'lines': [1, 2, 3, 4]}])
    def test_ignores_placeholders_round_figures_dates_and_skipped_columns(self):
        self.track('t.py', 'x = "2026-02-17 2750.00 12.34 999.99 202602 58.91"\n')
        result, report = self.run_scan()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(report['findings'], [])
    def test_untracked_files_are_not_scanned(self):
        (self.repo/'scratch.py').write_text('rate = 104.8762\n')
        result, _ = self.run_scan()
        self.assertEqual(result.returncode, 0)
    def test_source_inside_target_is_refused(self):
        inside = self.repo/'ledger.json'
        inside.write_text('{}')
        result = subprocess.run([sys.executable, str(SCRIPT), str(self.repo), '--source', str(inside)],
                                capture_output=True, text=True)
        self.assert_private(result)
        self.assertEqual(result.returncode, 2)
    def test_unreadable_source_is_incomplete(self):
        self.ledger.write_text('{not json')
        result, _ = self.run_scan()
        self.assertEqual(result.returncode, 2)

    def test_text_source_and_thresholds_use_worktree_content(self):
        source = self.ledger.with_suffix('.txt')
        source.write_text('amount 5847; rate 58.91\n')
        self.track('t.py', 'public example\n')
        (self.repo/'t.py').write_text('5847\n58.91\n')
        result, report = self.scan('--source', str(source), '--min-int', '1000', '--min-sig', '5')
        self.assertNotIn('5847', result.stdout + result.stderr)
        self.assertNotIn('58.91', result.stdout + result.stderr)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report['findings'], [{'path': 't.py', 'lines': [1]}])

    def test_values_file_alone_filters_each_line(self):
        values = self.ledger.with_suffix('.txt')
        values.write_text('# 104.8762\n\n287413.650\n120000\ntext 3189.45\n')
        self.track('t.py', '287413.65\n120000\n104.8762\n3189.45\n')
        result, report = self.scan('--values-file', str(values))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report['distinctive_source_values'], 1)
        self.assertEqual(report['findings'], [{'path': 't.py', 'lines': [1]}])

    def test_values_files_repeat_and_combine_with_sources(self):
        first = self.ledger.with_name('first.txt')
        second = self.ledger.with_name('second.txt')
        first.write_text('287413.65\n')
        second.write_text('37.4219\n')
        self.track('t.py', '287413.65\n37.4219\n104.8762\n')
        result, report = self.scan('--source', str(self.ledger), '--values-file', str(first),
                                   '--values-file', str(second))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report['findings'][0]['lines'], [1, 2, 3])

    def test_values_file_inside_target_or_unreadable_is_incomplete(self):
        inside = self.repo/'values.txt'
        inside.write_text('287413.65\n')
        link = self.ledger.with_name('values-link.txt')
        link.symlink_to(inside)
        for path in (inside, link, self.ledger.with_name('missing.txt')):
            with self.subTest(path=path.name):
                result, _ = self.scan('--values-file', str(path))
                self.assertEqual(result.returncode, 2)

    def test_source_or_values_file_is_required(self):
        result = subprocess.run([sys.executable, str(SCRIPT), str(self.repo)],
                                capture_output=True, text=True)
        self.assert_private(result)
        self.assertEqual(result.returncode, 2)

    def test_malformed_csv_is_incomplete(self):
        self.trades.write_text('amount\n28741365,104.8762\n')
        result, _ = self.scan('--source', str(self.trades))
        self.assertEqual(result.returncode, 2)

if __name__ == '__main__':
    unittest.main()
