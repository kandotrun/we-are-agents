from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from waa import cli


class DefaultStateTests(unittest.TestCase):
    def test_development_checkout_keeps_existing_state_path(self):
        with tempfile.TemporaryDirectory() as folder:
            checkout = Path(folder).resolve() / "checkout"
            with patch.object(cli, "ROOT", checkout), patch.dict(os.environ, {"WAA_HOME": ""}):
                self.assertEqual(cli._network_state(), checkout / ".local/network")

    def test_versioned_code_and_direct_executables_share_one_fixed_state_path(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder).resolve()
            for version in ("0.2.0", "0.2.1"):
                release = home / "releases" / version
                release.mkdir(parents=True)
                (release / "release.json").write_text("{}")
                with patch.object(cli, "ROOT", release), patch.dict(os.environ, {"WAA_HOME": ""}):
                    self.assertEqual(cli._network_state(), home / "state/network")
            explicit = home / "explicit-owner-installation"
            with patch.dict(os.environ, {"WAA_HOME": str(explicit)}):
                self.assertEqual(cli._network_state(), explicit / "state/network")


if __name__ == "__main__":
    unittest.main()
