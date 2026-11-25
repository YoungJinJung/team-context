"""Check that interactive controls refer to elements present in the document."""
from html.parser import HTMLParser
from pathlib import Path
import re


class Ids(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []

    def handle_starttag(self, tag, attrs):
        self.ids.extend(value for name, value in attrs if name == 'id')


def test_script_selectors_exist_and_ids_are_unique():
    html = (Path(__file__).resolve().parent.parent / 'index.html').read_text()
    parser = Ids()
    parser.feed(html)
    selectors = re.findall(r"getElementById\(['\"]([^'\"]+)['\"]\)", html)
    assert selectors
    assert len(parser.ids) == len(set(parser.ids))
    assert set(selectors) <= set(parser.ids)
