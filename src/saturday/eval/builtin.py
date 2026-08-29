from __future__ import annotations

from saturday.eval.runner import EvalCase, composite, file_created, regex_matches

PRIMES_BELOW_100 = 25


def builtin_suite(root: str | None = None) -> list[EvalCase]:
    return [
        EvalCase(
            id="builtin_write_file",
            task=(
                "In the current workspace, create a file named 'hello_saturday.txt' containing exactly "
                "the single line: Saturday online. Then read it back to verify."
            ),
            verifier=file_created("hello_saturday.txt", must_contain=("Saturday online",), root=root),
        ),
        EvalCase(
            id="builtin_math_reasoning",
            task=(
                "How many prime numbers are strictly less than 100? Verify your count with the python tool, "
                "then give a final answer of exactly one integer."
            ),
            verifier=composite(
                regex_matches(rf"\b{PRIMES_BELOW_100}\b"),
            ),
        ),
        EvalCase(
            id="builtin_self_report",
            task=(
                "Use the list_dir tool on the current directory, then report how many entries you see. "
                "Format the final line as: entries: N"
            ),
            verifier=regex_matches(r"entries:\s*\d+"),
        ),
    ]
