"""Evaluate a local combined JSON export without any trading or database access."""
import argparse
import json
from pathlib import Path

from core.research import load_export, evaluate_export


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--export', required=True, type=Path)
    parser.add_argument('--payout', default=0.80, type=float, help='Assumed net return on a win')
    parser.add_argument('--research-grid', action='store_true', help='Explore only the earlier period')
    parser.add_argument('--output', type=Path, help='New JSON report path; existing files are never overwritten')
    args = parser.parse_args()
    data, digest = load_export(args.export)
    report = evaluate_export(data, digest, args.payout, args.research_grid)
    content = json.dumps(report, indent=2, allow_nan=False)
    if args.output:
        with args.output.open('x') as output:
            output.write(content + '\n')
        print(f'Research-only report saved to {args.output}')
    else:
        print(content)


if __name__ == '__main__':
    main()
