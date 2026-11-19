"""Deploy-hook fakes keep the real Common Context.run call contract."""
from pathlib import Path
from unittest.mock import create_autospec

from boring_deploy import Context


def deploy_context(**fields):
    real = Context(release=None, home=Path("/nonexistent"), installed=None, log=lambda message: None)
    ctx = create_autospec(Context, instance=True)
    for name, value in (vars(real) | fields).items():
        if name == "run":
            ctx.run.side_effect = value
        else:
            setattr(ctx, name, value)
    return ctx
