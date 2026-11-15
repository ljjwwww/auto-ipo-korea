import io
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ipo_monitor


class SourcesTests(unittest.TestCase):
    def test_config_repeat_collection_and_disabled(self):
        feed = b'<rss version="2.0"><channel><title>x</title><link>https://example.com</link><description>x</description><item><guid>x</guid><title>x</title><link>https://example.com/x</link></item></channel></rss>'
        with tempfile.TemporaryDirectory() as d:
            config = Path(d) / 'sources.toml'
            config.write_text('[[sources]]\nname="test"\nurl="https://example.com/rss"\nenabled=true\n[[sources]]\nname="off"\nurl="https://example.com/off"\nenabled=false\n')
            args = ['collect', '--config', str(config), '--db', str(Path(d) / 'db')]
            with patch('ipo_monitor.read_feed', return_value=('https://example.com/rss', feed)) as fetch:
                outputs = []
                for _ in range(2):
                    out = io.StringIO()
                    with redirect_stdout(out):
                        self.assertEqual(ipo_monitor.main(args), 0)
                    outputs.append(out.getvalue())
                self.assertIn('기준 목록: 1건', outputs[0])
                self.assertIn('새 글: 0건', outputs[1])
                self.assertEqual(fetch.call_count, 2)

    def test_invalid_config_does_not_collect(self):
        with tempfile.TemporaryDirectory() as d:
            config = Path(d) / 'sources.toml'
            config.write_text('[[sources]]\nname="bad"\nurl="https://example.com"\nenabled="yes"\n')
            with patch('ipo_monitor.read_feed') as fetch, redirect_stderr(io.StringIO()):
                self.assertEqual(ipo_monitor.main(['collect', '--config', str(config)]), 1)
                fetch.assert_not_called()
