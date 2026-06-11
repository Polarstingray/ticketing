from calc import add, subtract, divide


def test_add():
    assert add(2, 3) == 5


def test_subtract():
    assert subtract(5, 2) == 3


def test_divide():
    assert divide(6, 2) == 3
