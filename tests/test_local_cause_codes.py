"""Literal Refused/Failed causes in src; dynamic calls have short reviewed exceptions."""
import ast
from pathlib import Path
from boring_outcomes import OUR_CODES

EXTRA_CODES = {
    "worker_failed": "an unexpected local worker operation failed; consult its log",
    "worker_interrupted": "the worker connection ended after dispatch; inspect retained work",
    "authority_failed": "an unexpected local authority operation failed; consult its log",
    "registration_failed": "an unexpected local Pirate registration operation failed; consult its log",
}
DYNAMIC_CODES = {
    ("src/boring_crocodile/auth_routes.py", "code"):
        "service_refusal forwards the caller-owned cause",
    ("src/boring_crocodile/outcomes.py", "code"):
        "typed carrier and refusal wrapper forward their caller-owned causes",
}


def test_local_cause_vocabulary():
    root = Path(__file__).resolve().parents[1]
    paths = sorted(root.glob("src/**/*.py"))
    assert paths and not OUR_CODES.keys() & EXTRA_CODES.keys()
    allowed, reviewed = OUR_CODES.keys() | EXTRA_CODES.keys(), set()
    for path in paths:
        for call in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if not isinstance(call, ast.Call):
                continue
            name = (call.func.id if isinstance(call.func, ast.Name) else
                    call.func.attr if isinstance(call.func, ast.Attribute) else "")
            if name not in ("Refused", "Failed"):
                continue
            code = next((kw.value for kw in call.keywords if kw.arg == "code"),
                        call.args[0] if call.args else None)
            where = f"{path.relative_to(root)}:{call.lineno}"
            if isinstance(code, ast.Constant):
                assert isinstance(code.value, str) and code.value in allowed, where
            else:
                key = (str(path.relative_to(root)), ast.unparse(code))
                assert key in DYNAMIC_CODES and DYNAMIC_CODES[key], where
                reviewed.add(key)
    assert reviewed == DYNAMIC_CODES.keys()
