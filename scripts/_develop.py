"""boring_deploy.develop (the shared development steps) and boring_deploy.artifact
from a Common dist: load() takes the folder, or this script's --common-wheels.
The wheel is unpacked to a temporary folder: artifact copies the engine's own files."""
import argparse
import atexit
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path


def load(common_wheels=None):
    """(artifact, develop) from the boring_deploy wheel among the Common wheels."""
    if common_wheels is None:
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--common-wheels", type=Path)
        common_wheels = parser.parse_known_args()[0].common_wheels
        if common_wheels is None:
            raise SystemExit("--common-wheels DIR is required: the Common dist (its wheels)")
    engine = sorted(Path(common_wheels).glob("boring_deploy-*.whl"))
    if len(engine) != 1:
        raise SystemExit(f"expected exactly one boring_deploy wheel in {common_wheels}")
    unpacked = tempfile.mkdtemp(prefix="boring-deploy-")
    atexit.register(shutil.rmtree, unpacked, True)
    zipfile.ZipFile(engine[0]).extractall(unpacked)
    sys.path.insert(0, unpacked)
    from boring_deploy import artifact, develop
    return artifact, develop
