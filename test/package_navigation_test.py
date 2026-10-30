"""Release-cache and dependency selection tests without Docker or compilation."""

import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    'package_navigation', Path(__file__).resolve().parents[1] / 'scripts/package_navigation.py')
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


class Packaging(unittest.TestCase):
    def test_obsolete_installed_resources_are_removed_without_deleting_current_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prefix = root / 'install'
            obsolete = prefix / 'share/local_planner/old.yaml'
            current = prefix / 'share/local_planner/new.yaml'
            release.write(obsolete, 'old')
            release.write(current, 'new')
            release.write(root / 'build/local_planner/install_manifest.txt', str(current) + '\n')
            with patch.object(release, 'PREFIX', prefix):
                installed = release.installed_files(root)
                self.assertEqual(installed, {str(current)})
                release.prune_obsolete({str(obsolete), str(current)}, installed)
                self.assertFalse(obsolete.exists())
                self.assertTrue(current.exists())
                with self.assertRaisesRegex(ValueError, 'invalid cached install path'):
                    release.prune_obsolete({str(root / 'outside')}, set())

    def test_selection_includes_transitive_consumers_but_not_unrelated_packages(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in release.SOURCES:
                dependencies = {'local_planner': ['inspection_interfaces'],
                                'multi_map_nav': ['inspection_interfaces'],
                                'faster_lio': ['livox_ros_driver2', 'ndt_omp', 'inspection_interfaces'],
                                'gridmapper': ['control_input_msgs']}.get(name, [])
                release.write(root / name / 'package.xml', '<package><name>' + name + '</name>' +
                              ''.join('<depend>' + v + '</depend>' for v in dependencies) + '</package>')
            self.assertEqual(release.selected_packages(['local_planner'], root), ['local_planner'])
            self.assertEqual(release.selected_packages(['livox_ros_driver2'], root),
                             ['faster_lio', 'livox_ros_driver2'])
            self.assertEqual(release.selected_packages(['inspection_interfaces'], root),
                             ['faster_lio', 'inspection_interfaces', 'local_planner', 'multi_map_nav'])

    def test_snapshot_keeps_unchanged_release_edits_and_handles_changed_deleted_inputs(self):
        with tempfile.TemporaryDirectory() as temp:
            source, target = Path(temp) / 'source', Path(temp) / 'copy'
            source.mkdir()
            subprocess.run(['git', 'init', '-q', str(source)], check=True)
            release.write(source / 'CMakeLists.txt', 'original')
            release.write(source / '.gitignore', 'secret.yaml\n')
            release.write(source / 'secret.yaml', 'excluded')
            first = release.snapshot(source, target)
            release.write(target / 'CMakeLists.txt', 'release adaptation')
            mtime = (target / 'CMakeLists.txt').stat().st_mtime_ns
            self.assertEqual(release.snapshot(source, target), first)
            self.assertEqual((target / 'CMakeLists.txt').stat().st_mtime_ns, mtime)
            self.assertEqual((target / 'CMakeLists.txt').read_text(), 'release adaptation')
            self.assertFalse((target / 'secret.yaml').exists())
            release.write(source / 'CMakeLists.txt', 'upstream change')
            self.assertNotEqual(release.snapshot(source, target), first)
            self.assertEqual((target / 'CMakeLists.txt').read_text(), 'upstream change')
            (source / 'CMakeLists.txt').unlink()
            release.snapshot(source, target)
            self.assertFalse((target / 'CMakeLists.txt').exists())

    def test_external_source_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'source'
            source.mkdir()
            subprocess.run(['git', 'init', '-q', str(source)], check=True)
            outside = Path(temp) / 'outside'
            outside.write_text('outside')
            (source / 'external').symlink_to(outside)
            with self.assertRaisesRegex(ValueError, 'symlink requires review'):
                release.snapshot(source, Path(temp) / 'copy')


if __name__ == '__main__':
    unittest.main()
