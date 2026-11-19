"""Controller startup loads exactly its generated enabled-Pond scope."""
from contextlib import redirect_stderr
import io
import os
from pathlib import Path
import pwd
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from boring_crocodile import admin, control_plane, site_check, site_model, site_render
from boring_crocodile.settings import Settings
from site_host import FakeHost
from test_site_model import description


class ControllerScopeTest(unittest.TestCase):
    def layout(self, root):
        main, ponds = description()
        config = root / "config.toml"
        config.write_text(site_model.toml_document(main))
        for name, document in ponds.items():
            target = root / "ponds" / name / "config.toml"
            target.parent.mkdir(parents=True)
            target.write_text(site_model.toml_document(document))
        sealed = root / "ponds/garden"
        sealed.chmod(0o700)
        (sealed / "config.toml").chmod(0o600)
        return config, main, ponds

    def test_controller_starts_without_opening_or_listing_sealed_parked_pond(self):
        with tempfile.TemporaryDirectory() as directory:
            config, _, _ = self.layout(Path(directory))
            read = site_model._read
            def own_only(path, owner):
                self.assertNotIn("garden", path.parts)
                return read(path, owner)
            server = SimpleNamespace(settings=SimpleNamespace(log_level="INFO"))
            with patch.dict(os.environ, BORING_CROCODILE_CONFIG=str(config)), \
                 patch("sys.argv", ["boring-crocodile-mcp", "--ponds", "pond"]), \
                 patch.object(Path, "iterdir", side_effect=AssertionError("controller enumerated namespace")), \
                 patch.object(site_model, "_read", side_effect=own_only), \
                 patch.object(control_plane, "create_http_app", return_value=(server, object())) as app, \
                 patch("boring_crocodile.controller_socket.take_listener") as listener, \
                 patch("uvicorn.Server") as uvicorn:
                control_plane.main()
                loaded = app.call_args.args[0]
                self.assertEqual(set(loaded.ponds), {"pond"})
                uvicorn.return_value.run.assert_called_once_with(
                    sockets=[listener.return_value.__enter__.return_value])
            for selected in ((), ("pond", "pond"), ("../garden",), ("garden",)):
                with self.subTest(selected=selected), self.assertRaises(ValueError):
                    Settings.load(config, ponds=selected)

    def test_broken_description_is_named_by_controller_status_site_check_and_render(self):
        for member in ("main", "garden"):
            for problem in ("syntax", "validation"):
                with self.subTest(member=member, problem=problem), tempfile.TemporaryDirectory() as directory:
                    host = FakeHost(Path(directory))
                    config, main, ponds = host.paths.config, host.main, host.ponds
                    path = config if member == "main" else host.paths.state / "ponds/garden/config.toml"
                    if problem == "syntax":
                        path.write_text("bad = @\n")
                        detail = "Invalid value (at line 1, column 7)"
                    else:
                        value = main if member == "main" else ponds[member]
                        value["unknown_field"] = True
                        path.write_text(site_model.toml_document(value))
                        detail = "unknown_field"
                    with patch.dict(os.environ, BORING_CROCODILE_CONFIG=str(config)), \
                         patch("sys.argv", ["controller", "--ponds", "pond", "garden"]), \
                         patch.object(control_plane, "create_http_app") as app, \
                         redirect_stderr(io.StringIO()) as error, self.assertRaises(SystemExit):
                        control_plane.main()
                    app.assert_not_called()
                    self.assertEqual(len(error.getvalue().splitlines()), 1, error.getvalue())
                    self.assertIn(str(path), error.getvalue())
                    self.assertIn(detail, error.getvalue())
                    with patch("sys.argv", ["admin", "--config", str(config), "status"]), \
                         redirect_stderr(io.StringIO()) as error, self.assertRaises(SystemExit):
                        admin.main()
                    self.assertIn(str(path), error.getvalue())
                    self.assertIn(detail, error.getvalue())
                    report = site_check.site_check(paths=host.paths, host=host)
                    self.assertIn(str(path), report["irregular"][0]["detail"])
                    self.assertIn(detail, report["irregular"][0]["detail"])
                    with self.assertRaises(site_model.DescriptionError) as render:
                        site_render.plan(paths=host.paths, host=host)
                    self.assertIn(str(path), str(render.exception))
                    self.assertIn(detail, str(render.exception))

    @unittest.skipUnless(os.geteuid() == 0,
        "requires root for real root-owned sealed declaration; native fixture covers this layout")
    def test_unprivileged_controller_loads_with_real_modes_and_exact_groups(self):
        users = []
        for user in pwd.getpwall():
            if user.pw_uid > 0 and all(user.pw_uid != u.pw_uid and user.pw_gid != u.pw_gid for u in users):
                users.append(user)
            if len(users) == 3:
                break
        if len(users) < 3:
            self.skipTest("no three unprivileged UID/GID bindings")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, main, ponds = self.layout(root)
            for binding, user in zip([main["site"]["controller"],
                                      ponds["pond"]["identity"], ponds["garden"]["identity"]], users):
                binding.update(uid=user.pw_uid, gid=user.pw_gid)
            config.write_text(site_model.toml_document(main))
            config.chmod(0o644)
            root.chmod(0o711)
            namespace = root / "ponds"
            os.chown(namespace, 0, users[0].pw_gid)
            namespace.chmod(0o751)
            for name, document in ponds.items():
                folder = namespace / name
                file = folder / "config.toml"
                file.write_text(site_model.toml_document(document))
                os.chown(folder, 0, users[1 if name == "pond" else 2].pw_gid)
                os.chown(file, 0, users[1 if name == "pond" else 2].pw_gid)
                folder.chmod(0o750)
                file.chmod(0o640)
            sealed = namespace / "garden"
            os.chown(sealed, 0, 0)
            os.chown(sealed / "config.toml", 0, 0)
            sealed.chmod(0o700)
            (sealed / "config.toml").chmod(0o600)
            reader, writer = os.pipe()
            child = os.fork()
            if child == 0:
                os.close(reader)
                try:
                    os.setgroups([users[1].pw_gid])
                    os.setgid(users[0].pw_gid)
                    os.setuid(users[0].pw_uid)
                    loaded = Settings.load(config, ponds=("pond",))
                    assert set(loaded.ponds) == {"pond"}
                    try:
                        (sealed / "config.toml").read_bytes()
                    except PermissionError:
                        pass
                    else:
                        raise AssertionError("sealed declaration readable")
                except BaseException as error:
                    os.write(writer, repr(error).encode())
                    os._exit(1)
                os._exit(0)
            os.close(writer)
            message = os.read(reader, 65536).decode()
            os.close(reader)
            _, status = os.waitpid(child, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0, message)
