from tiny_python_bug import add


def test_adds_two_positive_integers() -> None:
    assert add(2, 3) == 5
