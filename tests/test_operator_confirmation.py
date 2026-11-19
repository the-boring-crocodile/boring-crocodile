"""Operator confirmation journeys; every effect owner is synthetic."""
from contextlib import nullcontext, ExitStack, redirect_stderr, redirect_stdout
from functools import partial
import io
import os
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from boring_deploy import root_input

from boring_crocodile import approval, setup, site_setup, site_render, site_extend, site_pirates, admin


class Terminal(io.StringIO):
    def __init__(self, answer="", terminal=True):
        super().__init__(answer)
        self.terminal = terminal

    def isatty(self):
        return self.terminal


class ConfirmationTest(unittest.TestCase):
    def test_plan_is_shown_before_confirmation_and_stdout_stays_pipeable(self):
        errors, output = io.StringIO(), io.StringIO()
        proposal = {"plan_digest": "fresh", "changes": ["synthetic file"]}
        with patch.object(approval.sys, "stdin", Terminal("APPLY\n")), redirect_stderr(errors), redirect_stdout(output):
            self.assertEqual(approval.confirm_apply(proposal, "write the file"), "fresh")
        self.assertLess(errors.getvalue().index("synthetic file"), errors.getvalue().index("Type APPLY"))
        self.assertEqual(output.getvalue(), "")

    def test_piped_apply_cannot_confirm_and_wrong_answers_cancel(self):
        for answer, terminal in (("APPLY\n", False), ("yes\n", True), ("apply\n", True), ("", True)):
            with self.subTest(answer=answer, terminal=terminal), patch.object(approval.sys, "stdin", Terminal(answer, terminal)):
                with redirect_stderr(io.StringIO()), self.assertRaises(ValueError):
                    approval.confirm_apply({"plan_digest": "fresh"}, "write the file")

    def test_all_site_entrypoints_require_confirmation_before_effects(self):
        cases = [
            (setup, ["--config", "INPUT", "--apply"], "inspect", "boring_crocodile.setup_apply.apply",
             {"review": []}, {"status": "complete"}),
            (site_setup, ["--config", "INPUT", "--apply"], "plan", "boring_crocodile.site_setup.apply",
             {}, {"local_health": "healthy"}),
            (site_render, ["--apply"], "plan", "boring_crocodile.site_render.apply",
             {}, {"status": "rendered"}),
            (site_extend, ["--apply"], "plan", "boring_crocodile.site_extend.apply",
             {}, {"status": "extended"}),
            (site_pirates, ["parrot", "--account", "synthetic", "--ponds", "pond", "--apply"],
             "plan_add", "boring_crocodile.site_pirates.apply", {"status": "change"}, {"status": "admitted"}),
        ]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "input.json"
            path.write_text("{}")
            for module, args, planner, effect, extra, outcome in cases:
                for answer, terminal, applied in (("APPLY\n", True, True), ("no\n", True, False),
                                                   ("APPLY\n", False, False)):
                    with self.subTest(entry=module.__name__, answer=answer, terminal=terminal), ExitStack() as stack:
                        proposal = {"plan_digest": "fresh", **extra}
                        planned = proposal if module is setup else (proposal, {})
                        stack.enter_context(patch.object(module, planner, return_value=planned))
                        owner = stack.enter_context(patch(effect, return_value=outcome))
                        stack.enter_context(patch.object(approval.sys, "stdin", Terminal(answer, terminal)))
                        stack.enter_context(redirect_stdout(io.StringIO()))
                        errors = stack.enter_context(redirect_stderr(io.StringIO()))
                        stack.enter_context(patch("os.geteuid", return_value=0))
                        stack.enter_context(patch.object(setup, "root_input",
                                                        side_effect=partial(root_input, owner=os.getuid())))
                        if module is setup:
                            stack.enter_context(patch.object(setup.SetupInputs, "parse", return_value=object()))
                        elif module is site_setup:
                            stack.enter_context(patch.object(site_setup.ServiceInputs, "parse", return_value=object()))
                        if module in (site_setup, site_render, site_extend, site_pirates):
                            name = "review_summary" if module is site_setup else "review"
                            stack.enter_context(patch.object(module, name, return_value="synthetic review"))
                        if module is site_extend:
                            stack.enter_context(patch.object(module, "budget", return_value={}))
                            stack.enter_context(patch.object(module, "told", return_value="synthetic outcome"))
                        actual = [str(path) if arg == "INPUT" else arg for arg in args]
                        code = module.main("add", actual) if module is site_pirates else module.main(actual)
                        self.assertEqual(code, 0 if applied else 2)
                        self.assertEqual(owner.call_count, int(applied))
                        self.assertIn("plan_digest", errors.getvalue())

    def test_admin_plan_apply_routes_require_confirmation_before_effects(self):
        cases = [
            (["service-grants", "canvas", "--callers", "pond=reader", "--apply"], "service_grants"),
            (["service-endpoint", "canvas", "--url", "https://example.test/mcp", "--apply"], "service_endpoint"),
            (["oauth", "client-remove", "test-client", "--apply"], "client_remove"),
            (["oauth", "client-prune", "--apply"], "client_prune"),
            (["oauth", "revoke", "--pond", "pond", "--all-clients", "--apply"], "revoke"),
        ]
        for args, method in cases:
            for answer in ("APPLY\n", "no\n"):
                with self.subTest(command=args, answer=answer), ExitStack() as stack:
                    store = Mock()
                    proposal = {"plan_digest": "fresh", "changes": ["synthetic"]}
                    getattr(store, method).return_value = proposal
                    store.plan_revoke.return_value = proposal
                    settings = Mock(ponds={"pond": Mock(profile_id="profile")}, pirate_service_callers={})
                    stack.enter_context(patch.object(admin.os, "geteuid", return_value=0))
                    stack.enter_context(patch.object(admin.Settings, "load", return_value=settings))
                    stack.enter_context(patch.object(admin, "oauth_store", return_value=store))
                    stack.enter_context(patch.object(admin, "site_lock", return_value=nullcontext()))
                    stack.enter_context(patch.object(approval.sys, "stdin", Terminal(answer)))
                    stack.enter_context(redirect_stdout(io.StringIO()))
                    stack.enter_context(redirect_stderr(io.StringIO()))
                    parsed = admin.build_parser().parse_args(args)
                    dispatch = admin._oauth if args[0] == "oauth" else admin._services
                    if answer == "APPLY\n":
                        dispatch(parsed)
                        self.assertEqual(getattr(store, method).call_args.args[-1], "fresh")
                    else:
                        with self.assertRaisesRegex(ValueError, "not confirmed"):
                            dispatch(parsed)
                        # No call carrying an approval digest: the only read is the plan.
                        self.assertFalse(any(call.args and call.args[-1] == "fresh"
                                             for call in getattr(store, method).call_args_list))

    def test_uninstall_confirms_before_calling_owner(self):
        cases = [
            (["uninstall", "--site", "--delete-data", "--apply"], admin._uninstall,
             "boring_crocodile.uninstall.plan", "boring_crocodile.uninstall.apply"),
        ]
        for args, dispatch, planner, effect in cases:
            for answer in ("APPLY\n", "no\n"):
                with self.subTest(command=args, answer=answer), ExitStack() as stack:
                    stack.enter_context(patch(planner, return_value=({"plan_digest": "fresh"}, {})))
                    owner = stack.enter_context(patch(effect, return_value={"status": "done"}))
                    stack.enter_context(patch.object(admin.os, "geteuid", return_value=0))
                    stack.enter_context(patch.object(admin.Settings, "load", return_value=object()))
                    stack.enter_context(patch.object(approval.sys, "stdin", Terminal(answer)))
                    stack.enter_context(redirect_stdout(io.StringIO()))
                    stack.enter_context(redirect_stderr(io.StringIO()))
                    parsed = admin.build_parser().parse_args(args)
                    if answer == "APPLY\n":
                        dispatch(parsed)
                        owner.assert_called_once()
                        self.assertEqual(owner.call_args.kwargs["approved_digest"], "fresh")
                        self.assertTrue(owner.call_args.kwargs["delete_data"])
                    else:
                        with self.assertRaisesRegex(ValueError, "not confirmed"):
                            dispatch(parsed)
                        owner.assert_not_called()

    def test_removed_digest_option_is_rejected_by_parsers(self):
        for module, args in (
                (setup, ["--config", "unused"]), (site_setup, ["--config", "unused"]),
                (site_render, []), (site_extend, []),
                (site_pirates, ["parrot", "--account", "synthetic", "--ponds", "pond"])):
            with self.subTest(module=module.__name__), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    actual = args + ["--approve-digest", "old"]
                    module.main("add", actual) if module is site_pirates else module.main(actual)
                self.assertEqual(error.exception.code, 2)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            admin.build_parser().parse_args(["uninstall", "--site", "--apply", "--approve-digest", "old"])
