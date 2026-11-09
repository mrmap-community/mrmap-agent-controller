"""The repository as something a human installs: example config, launcher, units.

The controller's own tests build their config in code, so nothing else loads
``config.example.json`` -- and an example that does not validate is a broken
install on every host. Offline throughout: the only things executed here are
``bash -n`` and the CLI's own argument parser.
"""
import json
import os
import pathlib
import subprocess
import unittest

from mrmap_controller import config as config_mod

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: Switches where one of the values makes the controller act on a running agent.
#: A fresh install must never start in that value. Keys are checked only when
#: this revision knows them, so the table can outlive the package it describes.
ACTING_MODES = {
    'questions_enabled': ('off', 'report-only', 'send'),
}

#: Commands every revision of this repository offers; the CI and packaging check
#: is that the launcher reaches this package, not that a young command exists.
STABLE_COMMANDS = ('tick', 'status', 'logs', 'doctor', 'backup', 'release',
                   'pause', 'resume', 'loop')


class ExampleConfiguration(unittest.TestCase):
    def setUp(self):
        self.raw = json.loads((ROOT / 'config.example.json').read_text())

    def test_the_documented_example_passes_the_validator(self):
        cfg = config_mod.Config(dict(config_mod.DEFAULTS, **self.raw))
        config_mod._validate(cfg)          # must not raise

    def test_the_example_names_every_known_key_and_no_other(self):
        # install.sh copies this file verbatim, so an unknown key is a silent
        # typo and a missing key is a surprise for whoever edits it next.
        self.assertEqual(set(self.raw), set(config_mod.DEFAULTS))

    def test_a_fresh_install_is_report_only(self):
        # dry_run=true is the only thing standing between an install and a
        # controller that moves the shared board.
        self.assertIs(self.raw['dry_run'], True)
        self.assertLessEqual(self.raw['max_workers'], 3)

    def test_a_fresh_install_does_not_act_on_a_running_agent(self):
        for key, ladder in ACTING_MODES.items():
            if key not in config_mod.DEFAULTS:
                continue
            self.assertIn(self.raw[key], ladder, key)
            self.assertNotEqual(self.raw[key], ladder[-1],
                                f'{key} must be chosen, never inherited from the example')

    def test_the_example_keeps_the_bot_out_of_the_decision_lists(self):
        # The controller acts on a label whose last event came from an
        # allowlisted human, and treats reviews from the allowlist as feedback to
        # implement. The bot's own login in either list turns the loop into a
        # machine that approves and instructs itself.
        self.assertTrue(self.raw['ignore_authors'], 'the bot must name itself somewhere')
        for login in self.raw['ignore_authors']:
            self.assertNotIn(login, self.raw['approvers'], login)
            self.assertNotIn(login, self.raw['review_allowlist'], login)


class ShellSurface(unittest.TestCase):
    def test_the_installer_and_launcher_are_valid_executable_bash(self):
        for script in ('install.sh', 'bin/mrmap-controller'):
            path = ROOT / script
            self.assertTrue(path.is_file(), script)
            self.assertTrue(os.access(path, os.X_OK), f'{script} is not executable')
            proc = subprocess.run(['bash', '-n', str(path)],
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, f'{script}: {proc.stderr}')

    def test_the_launcher_runs_this_checkout_not_an_installed_copy(self):
        # A launcher that finds an installed copy elsewhere would let a fix land
        # in git and a run use the old code -- which is how a production
        # controller and its repository drift apart.
        proc = subprocess.run([str(ROOT / 'bin' / 'mrmap-controller'), '--help'],
                              capture_output=True, text=True,
                              env={**os.environ, 'HOME': str(ROOT),
                                   'MRMAP_CONTROLLER_CONFIG': '/nonexistent.json'})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for command in STABLE_COMMANDS:
            self.assertIn(command, proc.stdout, f'{command} vanished from the CLI')

    def test_the_units_describe_this_package_and_no_host_path(self):
        # install.sh copies the units after substituting @INSTALL@. A unit that
        # hardcodes one machine's home directory, or runs something other than
        # the launcher, is a second private definition of how the controller
        # starts -- and the first thing to rot when the install moves.
        units = list((ROOT / 'systemd').glob('*'))
        self.assertTrue(units, 'no unit files to install')
        for unit in units:
            text = unit.read_text()
            self.assertNotIn('/home/', text, f'{unit.name} hardcodes a host path')
            if unit.suffix == '.service':
                self.assertIn('@INSTALL@', text, unit.name)
                self.assertIn('bin/mrmap-controller', text, unit.name)
            if unit.suffix == '.timer':
                # OPERATIONS.md tells a human how often to expect a tick; the
                # unit that decides it must not quietly disagree.
                self.assertIn('OnUnitActiveSec=5min', text, unit.name)


class WorkflowDefinition(unittest.TestCase):
    """The CI definition, which the repository's own token may not install.

    A workflow runs with the repository's permissions, so the bot's token has no
    "workflows" scope and a human copies ci/ci.yml into .github/workflows/. These
    assertions keep that file honest while it waits, keep the two copies from
    drifting once it is activated, and keep CI out of being a path to production.
    """

    def copies(self):
        return [p for p in (ROOT / 'ci' / 'ci.yml',
                            ROOT / '.github' / 'workflows' / 'ci.yml') if p.is_file()]

    def test_the_definition_exists_in_git(self):
        self.assertTrue(self.copies(),
                        'ci/ci.yml must exist, activated or waiting to be activated')

    def test_an_activated_copy_cannot_differ_from_the_reviewed_one(self):
        copies = self.copies()
        if len(copies) < 2:
            return                      # not activated yet, nothing to compare
        first = copies[0].read_bytes()
        for other in copies[1:]:
            self.assertEqual(first, other.read_bytes(),
                             f'{other} drifted from {copies[0]}')

    def test_it_runs_the_suite_the_repository_documents(self):
        text = self.copies()[0].read_text()
        self.assertIn('python -m unittest discover -s tests', text)
        self.assertIn('compileall', text, 'modules no test imports must still compile')

    def test_it_asks_for_no_write_permission(self):
        text = self.copies()[0].read_text()
        self.assertIn('contents: read', text)
        self.assertNotIn(': write', text, 'CI may read this repository, nothing more')

    def test_ci_cannot_reach_the_deployment(self):
        # CI proving the code is not CI starting the code: no green check may
        # install, enable or run the real controller against the shared board.
        # install.sh appears in the definition only as `bash -n` -- parsing it is
        # the point, running it is the accident this guards against.
        text = self.copies()[0].read_text()
        for forbidden in ('systemctl', './install.sh', 'sh install.sh',
                          'bash install.sh', '--enable', '--execute',
                          'mrmap-controller tick'):
            self.assertNotIn(forbidden, text,
                             f'{forbidden} would turn a green check into a deployment')


if __name__ == '__main__':
    unittest.main()
