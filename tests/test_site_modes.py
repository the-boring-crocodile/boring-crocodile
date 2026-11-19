"""Description modes and ACL removal; native identities need root."""
import errno
import os
from pathlib import Path
import pwd
import stat
import struct
import tempfile
import unittest
from unittest.mock import patch

from boring_crocodile.site_host import Host, SiteConflict
from boring_crocodile.site_layout import description_modes, site_lock
from boring_crocodile.site_model import SiteDescription, toml_document
from site_host import FakeHost
from test_site_model import description


class DescriptionModesTest(unittest.TestCase):
    def test_real_modes_remove_stale_acls_keep_receipts_and_repair_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            host = FakeHost(Path(temporary))
            config = host.paths.config
            receipt = config.parent / "ponds/pond/setup.json"
            receipt.write_text("private legacy receipt")
            receipt.chmod(0o600)
            before = receipt.stat()
            # Linux POSIX ACL xattr: an old named UID with effective read access.
            acl = struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *entry) for entry in
                [(1,7,0xffffffff),(2,4,424242),(4,0,0xffffffff),(16,4,0xffffffff),(32,0,0xffffffff)])
            os.setxattr(config, "system.posix_acl_access", acl)
            os.setxattr(config.parent / "ponds", "system.posix_acl_default", acl)
            with site_lock(config.parent, owner=host.owner):
                rows = host.description_permissions(host.site, config)
                self.assertTrue(any(row["actual"] and row["actual"]["acl"] for row in rows))
                host.apply_description_permissions(rows)
            for row in rows:
                self.assertEqual(host.description_metadata(Path(row["path"])),
                    {key:row[key] for key in ("uid","gid","mode")} | {"acl":[]})
            self.assertEqual(receipt.stat(), before)
            self.assertEqual(receipt.read_text(), "private legacy receipt")
            config.unlink()
            config.write_text(toml_document(host.main))
            config.chmod(0o600)
            host.apply_description_permissions(host.description_permissions(host.site, config))
            self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o644)
            receipt.chmod(0o644)
            with self.assertRaisesRegex(SiteConflict, "Non-private"):
                host.description_permissions(host.site, config)

    def test_acl_errors_are_not_hidden_and_links_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            host = FakeHost(Path(temporary))
            config = host.paths.config
            with site_lock(config.parent, owner=host.owner):
                rows = host.description_permissions(host.site, config)
                with patch.object(host, "description_metadata", return_value={"acl":["system.posix_acl_access"]}), \
                     patch("os.removexattr", side_effect=OSError(errno.EACCES, "denied")), \
                     self.assertRaises(PermissionError):
                    host.apply_description_permissions(rows)
                target = config.parent / "ponds/pond/config.toml"
                target.unlink()
                target.symlink_to(config)
                with self.assertRaisesRegex(SiteConflict, "Unsafe description"):
                    host.apply_description_permissions(rows)

    @unittest.skipUnless(os.geteuid() == 0,
        "requires root to chown/switch identities; native fixture proves the same group layout")
    def test_real_worker_controller_and_stranger_access(self):
        users = []
        for user in pwd.getpwall():
            if user.pw_uid > 0 and all(user.pw_uid != u.pw_uid and user.pw_gid != u.pw_gid for u in users):
                users.append(user)
            if len(users) == 4:
                break
        if len(users) < 4:
            self.skipTest("no four distinct unprivileged UID/GID bindings")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o711)
            main, ponds = description()
            ponds["garden"]["enabled"] = True
            for binding, user in zip([main["site"]["controller"],
                                      ponds["pond"]["identity"], ponds["garden"]["identity"]], users):
                binding.update(uid=user.pw_uid, gid=user.pw_gid)
            site = SiteDescription.parse(main, ponds)
            config = root / "site/config.toml"
            config.parent.mkdir(mode=0o700)
            config.write_text(toml_document(main))
            for name, document in ponds.items():
                target = config.parent / "ponds" / name / "config.toml"
                target.parent.mkdir(parents=True)
                target.write_text(toml_document(document))
            receipt = config.parent / "ponds/pond/setup.json"
            receipt.write_text("private")
            receipt.chmod(0o600)
            host = Host(root)
            with site_lock(config.parent):
                host.apply_description_permissions(host.description_permissions(site, config))
            for role, user in zip(("controller","pond","garden","stranger"), users):
                reader, writer = os.pipe()
                child = os.fork()
                if child == 0:
                    os.close(reader)
                    try:
                        os.setgroups([u.pw_gid for u in users[1:3]] if role=="controller" else [])
                        os.setgid(user.pw_gid)
                        os.setuid(user.pw_uid)
                        from boring_crocodile.settings import Settings
                        if role != "stranger":
                            Settings.load(config, ponds=("pond","garden") if role=="controller" else None,
                                          environment=None if role=="controller" else role)
                        forbidden = receipt if role=="controller" else config.parent / (
                            "ponds/pond/config.toml" if role in ("garden","stranger") else "ponds/garden/config.toml")
                        try:
                            forbidden.read_bytes()
                        except PermissionError:
                            pass
                        else:
                            raise AssertionError("private neighbour/receipt readable")
                    except BaseException as error:
                        os.write(writer, repr(error).encode())
                        os._exit(1)
                    os._exit(0)
                os.close(writer)
                error = os.read(reader,65536).decode()
                os.close(reader)
                _, status = os.waitpid(child,0)
                self.assertEqual(os.waitstatus_to_exitcode(status),0,error)
