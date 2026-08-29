"""Demo module for harness capability tour."""

GREETING = "Hello from ox-alpha!"


def add(a: int, b: int) -> int:
    return a + b


def fib(n: int):
    seq = [0, 1]
    while len(seq) < n:
        seq.append(seq[-1] + seq[-2])
    return seq[:n]


# TODO: replace with real secret later
API_KEY_PLACEHOLDER = "sk-demo-REDACTED"
