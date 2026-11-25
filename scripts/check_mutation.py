"""Enforce a mutation score without counting errors as killed mutants."""
import json
import sys
from pathlib import Path


def score(stats):
    total = stats['total']
    killed = stats['killed']
    if total <= 0 or killed < 0 or killed > total:
        raise ValueError('Invalid mutation counts')
    return killed / total


def main(path):
    stats = json.loads(Path(path).read_text())
    value = score(stats)
    print(f"Mutation score: {stats['killed']}/{stats['total']} = {value:.2%}")
    if value < 0.90:
        raise SystemExit('Mutation score must be at least 90%')


if __name__ == '__main__':
    main(sys.argv[1])
