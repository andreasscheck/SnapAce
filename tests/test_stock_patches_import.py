"""Import-smoke tests for the two heavily-patched Snapmaker stock files.

klipper/kinematics/extruder.py and klipper/extras/filament_feed.py are
Snapmaker's own multi-thousand-line files with a handful of ACE-specific
hunks patched in (see original/ for the pre-patch baseline). They pull in
real Klipper/Snapmaker internals (`stepper`, `chelper`, `coded_exception`,
`queuefile`, the `extras` package for `filament_feed.py`'s relative
`pulse_counter` import) that only exist inside an actual Klipper checkout,
so full behavioral testing - constructing a real PrinterExtruder or
FilamentFeed and driving _cmd_SWITCH_EXTRUDER/the load-unload state
machine - isn't practical here without vendoring large parts of Klipper.

What we *can* cheaply check outside that environment: the files still
import cleanly. That catches the realistic failure mode for hand-applied
patches to someone else's large file - a bad merge, a stray indent, a
leftover conflict marker, a typo'd reference - without needing a real
printer. It does not catch logic bugs in the patched behavior itself.
"""
import os
import sys
import types
import unittest

REPO_ROOT = os.path.join(os.path.dirname(__file__), '..')
KLIPPER_DIR = os.path.join(REPO_ROOT, 'klipper')


class StockPatchImportTests(unittest.TestCase):
    def test_extruder_kinematics_module_imports(self):
        for name in ('stepper', 'chelper', 'coded_exception', 'queuefile'):
            sys.modules.setdefault(name, types.ModuleType(name))

        path = os.path.join(KLIPPER_DIR, 'kinematics', 'extruder.py')
        spec = self._load_spec('snapace_test_extruder', path)
        module = self._exec(spec)

        self.assertTrue(hasattr(module, 'PrinterExtruder'))

    def test_filament_feed_extras_module_imports(self):
        extras_pkg = types.ModuleType('extras')
        extras_pkg.__path__ = []
        sys.modules.setdefault('extras', extras_pkg)
        pulse_counter = types.ModuleType('extras.pulse_counter')
        sys.modules.setdefault('extras.pulse_counter', pulse_counter)
        extras_pkg.pulse_counter = pulse_counter

        path = os.path.join(KLIPPER_DIR, 'extras', 'filament_feed.py')
        spec = self._load_spec('extras.filament_feed', path)
        module = self._exec(spec)

        self.assertTrue(hasattr(module, 'FilamentFeed'))

    @staticmethod
    def _load_spec(name, path):
        import importlib.util
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise AssertionError(f'could not load spec for {path}')
        return spec

    @staticmethod
    def _exec(spec):
        import importlib.util
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module


if __name__ == '__main__':
    unittest.main()
