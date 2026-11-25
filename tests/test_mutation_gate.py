import json
import pytest
from scripts.check_mutation import main, score


def test_score():
    assert score({'total': 100, 'killed': 90}) == 0.9
    assert score({'total': 100, 'killed': 0}) == 0


@pytest.mark.parametrize('counts', [{'total': 0, 'killed': 0}, {'total': 10, 'killed': -1}, {'total': 10, 'killed': 11}])
def test_rejects_invalid_counts(counts):
    with pytest.raises(ValueError): score(counts)


def test_gate_counts_suspicious_as_not_killed(tmp_path, capsys):
    path = tmp_path / 'stats.json'
    path.write_text(json.dumps({'total': 100, 'killed': 90, 'suspicious': 10}))
    main(path)
    assert '90.00%' in capsys.readouterr().out
    path.write_text(json.dumps({'total': 100, 'killed': 89, 'suspicious': 11}))
    with pytest.raises(SystemExit, match='90%'): main(path)
