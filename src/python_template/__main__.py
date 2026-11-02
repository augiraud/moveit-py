"""Command line entry point: `python-template [COUNT]`."""

import argparse

from python_template.fizzbuzz import fizzbuzz


def main(argv: list[str] | None = None) -> None:
    """Print FizzBuzz from 1 to COUNT."""
    parser = argparse.ArgumentParser(description="Print FizzBuzz from 1 to COUNT.")
    parser.add_argument("count", type=int, nargs="?", default=15, help="last number (default: 15)")
    args = parser.parse_args(argv)
    for n in range(1, args.count + 1):
        print(fizzbuzz(n))


if __name__ == "__main__":
    main()
