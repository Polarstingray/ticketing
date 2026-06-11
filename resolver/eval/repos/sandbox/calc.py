"""A tiny calculator module used by the resolver eval fixtures."""


def add(a, b):
    return a + b


def subtract(a, b):
    return a - b


def divide(a, b):
    # NOTE: raises ZeroDivisionError on b == 0 (see the fix-divide-zero eval case).
    return a / b
