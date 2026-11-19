"""The public CLI describes its site operations and gives a usable next step."""
from unittest.mock import Mock

import pytest

from boring_crocodile import setup, setup_apply
from test_setup import example


def test_help_describes_site_configuration_and_selected_apply(capsys):
    with pytest.raises(SystemExit) as exit:
        setup.main(["--help"])
    assert exit.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "controller, listener and service policy" in help_text
    assert "Apply the selected operation" in help_text
    assert "initial account/context provisioning" not in help_text


def test_first_wizard_points_to_site_configuration_for_every_pond(monkeypatch, capsys):
    monkeypatch.setattr(setup.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(setup, "wizard", lambda *a, **k: setup.SetupInputs.parse(example()))
    monkeypatch.setattr(setup, "inspect", lambda *a, **k: {"review": []})
    monkeypatch.setattr(setup, "review_summary", lambda value: "synthetic provisioning plan")
    monkeypatch.setattr(setup, "confirm_apply", lambda *a: "reviewed-digest")
    apply = Mock(return_value={"status": "created"})
    monkeypatch.setattr(setup_apply, "apply", apply)
    assert setup.main(["--wizard", "--apply"]) == 0
    next_step = capsys.readouterr().err
    assert "--site-wizard --apply" in next_step
    assert "--site-config FILE --apply" in next_step
    assert "for a site that will take Pirates" not in next_step
    assert "INSTALL, Activate the site" not in next_step
    apply.assert_called_once()
