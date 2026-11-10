"""Offline checks for interactive exit and safe reuse of nginx's stock site."""
import hashlib
import importlib.util
import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('selfsteal', ROOT / 'selfsteal.py')
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


class MenuTests(unittest.TestCase):
    def test_exit_choices_finish_without_reopening_the_menu(self):
        for choice in ('3', '0', 'q', 'exit', 'выход'):
            with self.subTest(choice=choice), patch.object(app.os, 'geteuid', return_value=0, create=True), \
                 patch.object(app.shutil, 'which', return_value='/bin/tool'), \
                 patch.object(app, 'banner'), patch.object(app, 'info') as info, \
                 patch('builtins.input', return_value=choice), redirect_stdout(io.StringIO()) as out:
                app.main()
                self.assertEqual(info.call_count, 1)
                self.assertIn('Скрипт завершён', out.getvalue())

    def test_eof_and_keyboard_interrupt_exit(self):
        for exc in (EOFError, KeyboardInterrupt):
            with self.subTest(exc=exc), patch.object(app.os, 'geteuid', return_value=0, create=True), \
                 patch.object(app.shutil, 'which', return_value='/bin/tool'), \
                 patch.object(app, 'banner'), patch.object(app, 'info') as info, \
                 patch('builtins.input', side_effect=exc), redirect_stdout(io.StringIO()):
                app.main()
                self.assertEqual(info.call_count, 1)

    def test_no_color_and_noninteractive_output(self):
        with patch.object(app.sys.stdout, 'isatty', return_value=True), patch.dict(os.environ, {'NO_COLOR': '1'}):
            self.assertEqual(app.styled('текст', 'red'), 'текст')
        with patch.object(app.sys.stdout, 'isatty', return_value=False):
            self.assertNotIn('\x1b[', app.styled('текст', 'green'))

    def test_animation_is_shown_only_for_interactive_terminal(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True
        with patch.dict(os.environ, {'TERM': 'xterm'}, clear=True), \
             patch.object(app.time, 'sleep') as sleep, redirect_stdout(Terminal()) as out:
            app.banner()
            self.assertEqual(sleep.call_count, 4)
            self.assertIn('◐', out.getvalue())
        with patch.dict(os.environ, {'TERM': 'xterm', 'SELFSTEAL_NO_ANIMATION': '1'}, clear=True), \
             patch.object(app.time, 'sleep') as sleep, redirect_stdout(Terminal()):
            app.banner()
            sleep.assert_not_called()


class DefaultNginxTests(unittest.TestCase):
    def setUp(self):
        # Keep temporary files in the checkout so tests work in restricted environments.
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self.tmp.name) / 'nginx'
        for directory in ('sites-enabled', 'sites-available', 'conf.d'):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        self.main_conf = self.root / 'nginx.conf'
        self.site_conf = self.root / 'sites-available/default'
        self.enabled = self.root / 'sites-enabled/default'
        self.main_conf.write_text('http { include sites-enabled/*; }\n', encoding='utf-8')
        self.site_conf.write_text('server { listen 80; }\n', encoding='utf-8')
        self.enabled.write_text('test link placeholder', encoding='utf-8')
        self.hashes = {str(path): hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest()
                       for path in (self.main_conf, self.site_conf)}
        self.rows = ['LISTEN 0 511 0.0.0.0:80 0.0.0.0:* users:(("nginx",pid=12,fd=5))']
        self.html = '<html><title>Welcome to nginx!</title></html>'

        original_is_symlink = Path.is_symlink
        original_resolve = Path.resolve

        def is_symlink(path):
            return path == self.enabled or original_is_symlink(path)

        def resolve(path, *args, **kwargs):
            if path == self.enabled:
                return original_resolve(self.site_conf, *args, **kwargs)
            return original_resolve(path, *args, **kwargs)

        def fake_run(args, **kwargs):
            if args[0] == 'dpkg-query':
                text = '\n'.join(f' {path} {digest}' for path, digest in self.hashes.items())
            elif args[:2] == ['nginx', '-T']:
                text = f'# configuration file {self.main_conf}:\nhttp {{}}\n# configuration file {self.enabled}:\nserver {{}}\n'
            else:
                raise AssertionError(f'Unexpected external command: {args}')
            return subprocess.CompletedProcess(args, 0, stdout=text, stderr='')

        response = MagicMock()
        response.status = 200
        response.read.side_effect = lambda n: self.html.encode('utf-8')
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        self.patches = [patch.object(Path, 'is_symlink', is_symlink),
                        patch.object(Path, 'resolve', resolve),
                        patch.object(app.shutil, 'which', return_value='/bin/tool'),
                        patch.object(app, 'status_service', return_value=True),
                        patch.object(app, 'output', return_value='12'),
                        patch.object(app, 'listener_rows', side_effect=lambda port=None: self.rows),
                        patch.object(app.subprocess, 'run', side_effect=fake_run),
                        patch.object(app.urllib.request, 'build_opener', return_value=opener)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def check_rejected(self):
        ok, reason = app.inspect_default_nginx(self.root)
        self.assertFalse(ok, reason)

    def test_stock_site_is_accepted(self):
        self.assertEqual(app.inspect_default_nginx(self.root), (True, 'обнаружена стандартная страница nginx'))

    def test_other_process_is_rejected(self):
        self.rows.append('LISTEN 0 128 [::]:80 [::]:* users:(("caddy",pid=9,fd=3))')
        self.check_rejected()

    def test_nginx_on_another_port_is_rejected(self):
        self.rows.append('LISTEN 0 128 0.0.0.0:8080 0.0.0.0:* users:(("nginx",pid=12,fd=5))')
        with patch.object(app, 'listener_rows', side_effect=lambda port=None: self.rows if port is None else self.rows[:1]):
            self.check_rejected()

    def test_nginx_from_another_service_is_rejected(self):
        self.rows = ['LISTEN 0 511 0.0.0.0:80 0.0.0.0:* users:(("nginx",pid=99,fd=5))']
        self.check_rejected()

    def test_other_site_is_rejected(self):
        (self.root / 'sites-enabled/other').write_text('custom', encoding='utf-8')
        self.check_rejected()

    def test_additional_config_is_rejected(self):
        (self.root / 'conf.d/app.conf').write_text('server {}', encoding='utf-8')
        self.check_rejected()

    def test_modified_packaged_config_is_rejected(self):
        self.site_conf.write_text('server { listen 80; proxy_pass http://example; }', encoding='utf-8')
        self.check_rejected()

    def test_custom_page_is_rejected(self):
        self.html = '<html><title>My website</title></html>'
        self.check_rejected()


class ReusePreflightTests(unittest.TestCase):
    def test_user_can_decline_without_changes(self):
        with patch.object(app, 'CURRENT', MagicMock(**{'exists.return_value': False})), \
             patch.object(app, 'is_listening', side_effect=lambda port: port == 80), \
             patch.object(app, 'inspect_default_nginx', return_value=(True, 'stock')), \
             patch.object(app, 'confirm', return_value=False), \
             patch.object(app, 'run') as run, patch.object(app, 'input_parameters') as inputs, \
             redirect_stdout(io.StringIO()):
            app.install()
            run.assert_not_called()
            inputs.assert_not_called()

    def test_changed_configuration_stops_before_backup(self):
        with patch.object(app, 'CURRENT', MagicMock(**{'exists.return_value': False})), \
             patch.object(app, 'is_listening', side_effect=lambda port: port == 80), \
             patch.object(app, 'inspect_default_nginx', side_effect=[(True, 'stock'), (False, 'changed')]), \
             patch.object(app, 'confirm', return_value=True), \
             patch.object(app, 'input_parameters', return_value=('test.example.com', '192.0.2.1', 'a@example.com')), \
             patch.object(app.shutil, 'which', return_value='/bin/tool'), \
             patch.object(app, 'ufw_active', return_value=True), \
             patch.object(app, 'run') as run, redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'изменилась'):
                app.install()
            run.assert_not_called()

    def test_confirmed_default_nginx_is_saved_before_install_and_restored(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            state_dir = root / 'state'
            default = root / 'nginx/sites-enabled/default'
            default.parent.mkdir(parents=True)
            default.write_text('original default site', encoding='utf-8')
            paths = {'STATE': state_dir, 'SNAPSHOTS': state_dir / 'snapshots',
                     'CURRENT': state_dir / 'current.json', 'SITE': root / 'site',
                     'DEFAULT': default, 'CONF': root / 'nginx/sites-available/selfsteal.conf',
                     'ENABLED': root / 'nginx/sites-enabled/selfsteal.conf',
                     'HOOK': root / 'hook.sh', 'PROFILE': state_dir / 'inbound.json'}
            version = subprocess.CompletedProcess(['nginx', '-v'], 0, stdout='', stderr='nginx/1.24.0')
            with patch.multiple(app, **paths), \
                 patch.object(app, 'is_listening', side_effect=lambda port: port == 80), \
                 patch.object(app, 'inspect_default_nginx', return_value=(True, 'stock')) as inspect, \
                 patch.object(app, 'confirm', return_value=True), \
                 patch.object(app, 'input_parameters', return_value=('test.example.com', '192.0.2.1', 'a@example.com')), \
                 patch.object(app.shutil, 'which', return_value='/bin/tool'), \
                 patch.object(app, 'ufw_active', return_value=True), \
                 patch.object(app, 'status_service', return_value=True), \
                 patch.object(app, 'ssh_ports', return_value=[22]), \
                 patch.object(app, 'run', return_value=subprocess.CompletedProcess([], 0)) as run, \
                 patch.object(app, 'write_conf'), patch.object(app, 'render_site', return_value='test'), \
                 patch.object(app, 'make_profile', return_value=('public', 'short')), \
                 patch.object(app, 'check_report'), patch.object(app, 'delete_ufw_rules'), \
                 patch.object(app.subprocess, 'run', return_value=version), redirect_stdout(io.StringIO()):
                app.install()
                self.assertEqual(inspect.call_count, 2)
                state = app.read_state()
                self.assertTrue(state['nginx_was_active'])
                self.assertFalse(default.exists())
                self.assertTrue(app.rollback(state))
                self.assertEqual(default.read_text(encoding='utf-8'), 'original default site')
                self.assertIn((('systemctl', 'restart', 'nginx'), {'check': False}),
                              [(call.args, call.kwargs) for call in run.call_args_list])


class RollbackTests(unittest.TestCase):
    def test_restores_default_site_and_restarts_previously_active_nginx(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            backup = root / 'snapshot'
            backup.mkdir()
            default = root / 'sites-enabled/default'
            default.parent.mkdir()
            default.write_text('previous default', encoding='utf-8')
            record = app.save_path(default, backup, 0)
            default.write_text('changed by install', encoding='utf-8')
            current = root / 'current.json'
            current.write_text('{}', encoding='utf-8')
            state = {'backup': str(backup), 'paths': [record], 'marker': 'test',
                     'nginx_was_active': True, 'nginx_was_enabled': True,
                     'ufw_was_active': False, 'cert_created': False}
            with patch.object(app, 'CURRENT', current), \
                 patch.object(app, 'delete_ufw_rules'), \
                 patch.object(app.shutil, 'which', side_effect=lambda name: '/bin/nginx' if name == 'nginx' else None), \
                 patch.object(app, 'run', return_value=subprocess.CompletedProcess([], 0)) as run, \
                 redirect_stdout(io.StringIO()):
                self.assertTrue(app.rollback(state))
                self.assertEqual(default.read_text(encoding='utf-8'), 'previous default')
                self.assertFalse(current.exists())
                self.assertIn((('systemctl', 'restart', 'nginx'), {'check': False}),
                              [(call.args, call.kwargs) for call in run.call_args_list])


if __name__ == '__main__':
    unittest.main()
