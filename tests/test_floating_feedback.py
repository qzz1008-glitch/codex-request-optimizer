"""Reviewed feedback stays within stable inline areas, including dialogs and tray."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from html.parser import HTMLParser

ROOT = Path(__file__).resolve().parents[1]


class FeedbackMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.uncovered = []
        self.stack = []
        self.unrouted = []
        self.identifiers = set()
        self.targets = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        covered = 'data-feedback-source' in attrs or 'data-feedback-inline' in attrs or any(self.stack)
        identifier = attrs.get('id', '')
        if identifier:
            self.identifiers.add(identifier)
        if 'data-feedback-source' in attrs:
            target = attrs.get('data-feedback-target', '')
            if target:
                self.targets.add(target)
            else:
                self.unrouted.append(identifier or attrs.get('class', tag))
        if (identifier.endswith('-message') or identifier in {'notice', 'tray-error', 'list-loading', 'settings-save-log'}) and not covered:
            self.uncovered.append(identifier)
        if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}:
            self.stack.append(covered)

    def handle_endtag(self, tag):
        if self.stack:
            self.stack.pop()


class FloatingFeedbackTests(unittest.TestCase):
    def test_all_feedback_has_an_explicit_presentation(self):
        for name in ('index.html', 'tray.html'):
            with self.subTest(name=name):
                markup = FeedbackMarkup()
                markup.feed((ROOT / 'packet_inspector/ui' / name).read_text(encoding='utf8'))
                self.assertEqual(markup.uncovered, [])
                self.assertEqual(markup.unrouted, [])
                self.assertLessEqual(markup.targets, markup.identifiers)

    @unittest.skipUnless(shutil.which('node'), 'Node.js is needed for browser contracts')
    def test_inline_results_validation_copy_and_stable_layout_in_a_browser(self):
        result = subprocess.run([shutil.which('node'), 'tests/floating_feedback.cjs'], cwd=ROOT,
                                capture_output=True, text=True, encoding='utf8', timeout=55)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        if 'skipped' in summary:
            self.skipTest(summary['skipped'])
        self.assertEqual(summary['floating_feedback'], 'passed')
        self.assertGreaterEqual(summary['browser_cases'], 9)
