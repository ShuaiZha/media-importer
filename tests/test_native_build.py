from pathlib import Path
import unittest
from unittest.mock import patch

from scripts import build_native


class NativeSigningTests(unittest.TestCase):
    def test_failed_build_removes_its_temporary_program_copy(self):
        created = []
        def fail(work, output, identity):
            created.append(work)
            (work / 'partial-build').write_bytes(b'partial program')
            raise RuntimeError('test build failure')
        with patch.object(build_native, 'build_in', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'test build failure'):
                build_native.build(Path('/unused'))
        self.assertEqual(1, len(created))
        self.assertFalse(created[0].exists())

    def test_local_build_has_a_fixed_identifier_but_no_relaxed_requirement(self):
        with patch.object(build_native.sys, 'platform', 'darwin'), patch.object(build_native.subprocess, 'run') as run:
            build_native.sign_macos(Path('/example/bundle'))
        command = run.call_args_list[0].args[0]
        self.assertIn(build_native.MACOS_IDENTIFIER, command)
        self.assertEqual('-', command[command.index('--sign') + 1])
        self.assertNotIn('--requirements', command)
        self.assertEqual('--verify', run.call_args_list[1].args[0][1])

    def test_optional_apple_identity_is_not_replaced_by_ad_hoc_signing(self):
        identity = 'Developer ID Application: Example (EXAMPLE)'
        with patch.object(build_native.sys, 'platform', 'darwin'), patch.object(build_native.subprocess, 'run') as run:
            build_native.sign_macos(Path('/example/bundle'), identity)
        command = run.call_args_list[0].args[0]
        self.assertEqual(identity, command[command.index('--sign') + 1])
        self.assertIn('--timestamp', command)
        self.assertIn('runtime', command)

    def test_signing_options_fail_on_other_platforms_before_building(self):
        with patch.object(build_native.sys, 'platform', 'linux'), patch.object(build_native.subprocess, 'run') as run:
            build_native.sign_macos(Path('/example/bundle'))
            with self.assertRaises(ValueError):
                build_native.build(Path('/unused'), 'example-signing-identity')
            run.assert_not_called()
