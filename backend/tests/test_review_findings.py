"""The reviewer's findings as the host accepts them."""
import json

from app.review.activities import read_findings

PREVIOUS = [{'number': 1, 'summary': 'GET /rooms  does not sort.', 'target': 'implementation',
             'path': 'service/Rooms.java'},
            {'number': 2, 'summary': 'POST /rooms accepts no name.', 'target': 'tests'}]


def findings(tmp_path, items, previous=PREVIOUS):
    (tmp_path / 'findings.json').write_text(json.dumps({'findings': items}))
    return read_findings(tmp_path, {'service/Rooms.java'}, previous)


def test_findings_are_marked_with_the_earlier_finding_they_repeat(tmp_path):
    base = {'target': 'implementation', 'reason': 'service', 'path': 'service/Rooms.java'}
    result = findings(tmp_path, [
        # Named by the reviewer.
        {**base, 'summary': 'Rooms are still unsorted.', 'repeats': 2},
        # Reported in the same words, though the reviewer did not say so.
        {**base, 'summary': 'GET /rooms does not   sort.'},
        # A number no earlier finding has, and a new finding.
        {**base, 'summary': 'GET /rooms pages wrongly.', 'repeats': 7},
    ])
    assert [item.get('repeats') for item in result] == [2, 1, None]
    # Without earlier findings nothing repeats.
    assert all('repeats' not in item for item in findings(tmp_path, [
        {**base, 'summary': 'GET /rooms does not sort.', 'repeats': 1}], previous=None))
