import pytest

from python_template.__main__ import main
from python_template.fizzbuzz import fizzbuzz


@pytest.mark.parametrize(
    ("n", "expected"),
    [(1, "1"), (3, "Fizz"), (5, "Buzz"), (15, "FizzBuzz"), (98, "98")],
)
def test_fizzbuzz(n: int, expected: str) -> None:
    assert fizzbuzz(n) == expected


def test_main_prints_one_line_per_number(capsys: pytest.CaptureFixture[str]) -> None:
    main(["5"])
    assert capsys.readouterr().out.splitlines() == ["1", "2", "Fizz", "4", "Buzz"]
