#!/usr/bin/env python3
"""Install a local HTTPS REALITY target without changing the Remnawave panel."""
import base64
import datetime as dt
import hashlib
import ipaddress
import json
import os
import random
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
from pathlib import Path

os.environ['LC_ALL'] = 'C'
ROOT = Path(__file__).resolve().parent
STATE = Path('/var/lib/selfsteal-installer')
SNAPSHOTS = STATE / 'snapshots'
CURRENT = STATE / 'current.json'
SITE = Path('/var/www/selfsteal')
CONF = Path('/etc/nginx/sites-available/selfsteal-installer.conf')
ENABLED = Path('/etc/nginx/sites-enabled/selfsteal-installer.conf')
DEFAULT = Path('/etc/nginx/sites-enabled/default')
HOOK = Path('/etc/letsencrypt/renewal-hooks/deploy/selfsteal-installer-nginx.sh')
PROFILE = STATE / 'reality-inbound.json'
SSH_MARK = 'selfsteal-installer-'


def styled(text, color='cyan', bold=False):
    """Use ANSI styling only on capable interactive terminals."""
    if not sys.stdout.isatty() or os.environ.get('TERM') == 'dumb' or 'NO_COLOR' in os.environ:
        return str(text)
    colors = {'cyan': '36', 'green': '32', 'yellow': '33', 'red': '31',
              'white': '37', 'muted': '90'}
    return f'\033[{"1;" if bold else ""}{colors.get(color, "36")}m{text}\033[0m'


def heading(title):
    print('\n' + styled('  ──  ' + title + '  ' + '─' * 30, 'cyan', True))


def notice(message, kind='info'):
    icon, color = {'info': ('i', 'cyan'), 'ok': ('✓', 'green'),
                   'warn': ('!', 'yellow'), 'error': ('×', 'red')}[kind]
    print(f'  {styled("[" + icon + "]", color, True)} {message}')


def prompt(message):
    return input(styled('  › ', 'cyan', True) + message)


def confirm(message):
    return prompt(message + ' [да/нет]: ').strip().casefold() in ('y', 'yes', 'д', 'да')


def banner():
    logo = [
        r'   ____       _  __     _             _ ',
        r'  / ___|  ___| |/ _|___| |_ ___  __ _| |',
        r'  \___ \ / _ \ | |_/ __| __/ _ \/ _` | |',
        r'   ___) |  __/ |  _\__ \ ||  __/ (_| | |',
        r'  |____/ \___|_|_| |___/\__\___|\__,_|_|',
    ]
    print()
    for line in logo:
        print(styled(line, 'cyan', True))
    print(styled('  REALITY  /  SERVER SETUP', 'muted'))
    if (sys.stdout.isatty() and os.environ.get('TERM') != 'dumb'
            and not os.environ.get('SELFSTEAL_NO_ANIMATION')):
        for frame in ('◐', '◓', '◑', '◒'):
            print('\r' + styled('  ' + frame + '  Подготовка интерфейса', 'cyan'), end='', flush=True)
            time.sleep(0.06)
        print('\r' + ' ' * 38 + '\r', end='', flush=True)


def run(*args, check=True, capture=False, input_text=None):
    print(styled('  $ ', 'muted') + ' '.join(str(x) for x in args), flush=True)
    return subprocess.run(args, check=check, text=True, input=input_text,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)


def output(*args):
    p = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return p.stdout.strip() if p.returncode == 0 else ''


def atomic_json(path, obj, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.tmp-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            if hasattr(os, 'fchmod'):
                os.fchmod(f.fileno(), mode)
            else:
                os.chmod(tmp, mode)
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.write('\n')
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_state():
    return json.loads(CURRENT.read_text(encoding='utf-8'))


def save_state(state):
    atomic_json(CURRENT, state)


def status_service(name, action):
    return subprocess.run(['systemctl', action, '--quiet', name],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def listener_rows(port=None):
    p = subprocess.run(['ss', '-H', '-lntp'], text=True, capture_output=True, check=True)
    rows = [row for row in p.stdout.splitlines() if len(row.split()) >= 4]
    return [row for row in rows if row.split()[3].rsplit(':', 1)[-1] == str(port)] if port else rows


def is_listening(port):
    return bool(listener_rows(port))


def packaged_nginx_configs_match(nginx_root=Path('/etc/nginx')):
    """Require the two Debian/Ubuntu conffiles to match their installed package hashes."""
    query = subprocess.run(['dpkg-query', '-W', '-f=${Conffiles}', 'nginx-common'],
                           text=True, capture_output=True, check=False)
    if query.returncode:
        return False
    hashes = {}
    for line in query.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and re.fullmatch(r'[0-9a-fA-F]{32}', parts[1]):
            hashes[parts[0]] = parts[1].lower()
    for path in (nginx_root / 'nginx.conf', nginx_root / 'sites-available/default'):
        if str(path) not in hashes or not path.is_file():
            return False
        # MD5 is used here only to compare against dpkg's own conffile database.
        if hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest() != hashes[str(path)]:
            return False
    return True


def inspect_default_nginx(nginx_root=Path('/etc/nginx')):
    """Recognise a stock Debian/Ubuntu nginx setup conservatively, without changing it."""
    if not shutil.which('nginx') or not shutil.which('dpkg-query'):
        return False, 'nginx или dpkg-query не найден'
    if not status_service('nginx', 'is-active'):
        return False, 'системная служба nginx не активна'
    rows = listener_rows(80)
    if not rows:
        return False, 'порт 80 больше не занят'
    main_pid = output('systemctl', 'show', 'nginx', '--property=MainPID', '--value')
    if not main_pid.isdigit() or int(main_pid) <= 0:
        return False, 'не удалось определить процесс системной службы nginx'
    for row in rows:
        processes = re.findall(r'"([^"\n]+)"', row)
        if not processes or any(name != 'nginx' for name in processes):
            return False, 'порт 80 занимает не только системный nginx'
        if main_pid not in re.findall(r'pid=(\d+)', row):
            return False, 'порт 80 не принадлежит системной службе nginx'
    for row in listener_rows():
        processes = re.findall(r'"([^"\n]+)"', row)
        if 'nginx' in processes and row.split()[3].rsplit(':', 1)[-1] != '80':
            return False, 'nginx также слушает другие порты; проверьте его действующую конфигурацию'
    enabled_dir = nginx_root / 'sites-enabled'
    default = enabled_dir / 'default'
    if not default.is_symlink() or not default.exists():
        return False, 'стандартный сайт nginx не подключён обычной символьной ссылкой'
    if default.resolve() != (nginx_root / 'sites-available/default').resolve():
        return False, 'ссылка default указывает на нестандартный файл'
    if {p.name for p in enabled_dir.iterdir()} != {'default'}:
        return False, 'обнаружены другие файлы в sites-enabled'
    conf_dir = nginx_root / 'conf.d'
    if conf_dir.is_dir() and any(conf_dir.glob('*.conf')):
        return False, 'обнаружены дополнительные настройки в conf.d'
    if not packaged_nginx_configs_match(nginx_root):
        return False, 'конфигурация nginx отличается от файлов пакета или их контрольные суммы недоступны'
    config = subprocess.run(['nginx', '-T'], text=True, capture_output=True, check=False)
    if config.returncode:
        return False, 'nginx -T сообщает об ошибке'
    loaded = re.findall(r'^# configuration file (.+):$', config.stdout, re.MULTILINE)
    required = {str(nginx_root / 'nginx.conf'), str(default)}
    if not required.issubset(set(loaded)):
        return False, 'nginx использует нестандартный набор конфигурационных файлов'
    for path in loaded:
        if path in required or path == str(nginx_root / 'mime.types'):
            continue
        module_path = Path(path)
        if module_path.parent != nginx_root / 'modules-enabled':
            return False, 'nginx загружает дополнительную конфигурацию: ' + path
        try:
            lines = [line.split('#', 1)[0].strip() for line in module_path.read_text(encoding='utf-8').splitlines()]
        except OSError:
            return False, 'не удалось прочитать конфигурацию модуля: ' + path
        if any(line and not re.fullmatch(r'load_module\s+[^;]+;', line) for line in lines):
            return False, 'обнаружена дополнительная конфигурация модуля: ' + path
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open('http://127.0.0.1/', timeout=5) as response:
            body = response.read(65536).decode('utf-8', errors='replace')
            if response.status != 200 or '<title>Welcome to nginx!</title>' not in body:
                return False, 'на 127.0.0.1:80 отображается не стандартная страница nginx'
    except (OSError, urllib.error.URLError):
        return False, 'не удалось проверить страницу nginx на 127.0.0.1:80'
    return True, 'обнаружена стандартная страница nginx'


def info():
    heading('ИНФОРМАЦИЯ О СЕРВЕРЕ')
    os_name = output('bash', '-c', '. /etc/os-release; printf "%s" "$PRETTY_NAME"')
    nginx_version = 'не установлен'
    if shutil.which('nginx'):
        version = subprocess.run(['nginx', '-v'], text=True, capture_output=True, check=False)
        nginx_version = (version.stderr or version.stdout).strip() or 'установлен'
    print(f'  Узел        {socket.gethostname()}')
    print(f'  ОС          {os_name}')
    print(f'  Ядро        {output("uname", "-r")}')
    print(f'  IP-адреса   {output("hostname", "-I") or "не определены"}')
    print(f'  nginx       {nginx_version}')
    print(f'  Снимок      {str(CURRENT) if CURRENT.exists() else "нет активной установки"}')
    print()
    for port in (80, 443, 8443):
        rows = listener_rows(port)
        status = styled('занят', 'yellow') if rows else styled('свободен', 'green')
        print(f'  TCP {port:<5}  {status}')
        for row in rows:
            processes = re.findall(r'"([^"\n]+)"', row)
            print('             ' + styled(', '.join(sorted(set(processes))) or row.strip(), 'muted'))


def valid_domain(domain):
    return (len(domain) <= 253 and '.' in domain and
            all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part)
                for part in domain.split('.')))


def server_ip():
    try:
        with urllib.request.urlopen('https://api.ipify.org', timeout=8) as response:
            return str(ipaddress.IPv4Address(response.read(100).decode().strip()))
    except Exception:
        return ''


def input_parameters():
    heading('ДОМЕН И СЕРТИФИКАТ')
    domain = prompt('Домен (A-запись на этот сервер): ').strip().lower().rstrip('.')
    if not valid_domain(domain):
        raise ValueError('Некорректный домен. Используйте полное имя в ASCII, например node.example.com.')
    suggested = server_ip()
    raw_ip = prompt(f'Публичный IPv4 сервера [{suggested}]: ').strip() or suggested
    ip = str(ipaddress.IPv4Address(raw_ip))
    email = prompt('Email для Let\'s Encrypt: ').strip()
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
        raise ValueError('Укажите корректный email.')
    addresses = {row[4][0] for row in socket.getaddrinfo(domain, 80, type=socket.SOCK_STREAM)}
    if ip not in addresses:
        raise ValueError(f'A-запись {domain} не содержит {ip}. DNS сейчас: {sorted(addresses)}')
    if any(':' in address for address in addresses):
        raise ValueError('У домена есть AAAA-запись. Этот установщик поддерживает HTTP-01 только через IPv4; удалите AAAA или используйте другую конфигурацию.')
    if len({a for a in addresses if ':' not in a}) != 1:
        raise ValueError('У домена несколько A-записей: выпуск может попасть на другой сервер.')
    return domain, ip, email


def save_path(path, backup, index):
    rec = {'path': str(path), 'type': 'missing', 'item': str(index)}
    dest = backup / str(index)
    if path.is_symlink():
        rec.update(type='link', target=os.readlink(path))
    elif path.is_dir():
        shutil.copytree(path, dest, symlinks=True)
        rec['type'] = 'directory'
    elif path.exists():
        shutil.copy2(path, dest, follow_symlinks=False)
        rec['type'] = 'file'
    return rec


def remove_path(path):
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def restore_path(rec, backup):
    path = Path(rec['path'])
    remove_path(path)
    if rec['type'] == 'missing':
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if rec['type'] == 'link':
        path.symlink_to(rec['target'])
    elif rec['type'] == 'directory':
        shutil.copytree(backup / rec['item'], path, symlinks=True)
    else:
        shutil.copy2(backup / rec['item'], path)


def ufw_active():
    return output('ufw', 'status').lower().startswith('status: active')


def ssh_ports():
    ports = set()
    conn = os.environ.get('SSH_CONNECTION', '').split()
    if len(conn) == 4 and conn[3].isdigit():
        ports.add(int(conn[3]))
    text = output('sshd', '-T') if shutil.which('sshd') else ''
    for line in text.splitlines():
        if re.fullmatch(r'port \d+', line):
            ports.add(int(line.split()[1]))
    # On newer Ubuntu installations systemd can own the SSH listener.
    socket_desc = output('systemctl', 'show', 'ssh.socket', '--property=Listen', '--value')
    ports.update(int(p) for p in re.findall(r':(\d+)\s*\(Stream\)', socket_desc))
    for row in output('ss', '-H', '-lntp').splitlines():
        fields = row.split()
        if len(fields) >= 4 and re.search(r'"sshd(?:\:|\")', row):
            candidate = fields[3].rsplit(':', 1)[-1]
            if candidate.isdigit():
                ports.add(int(candidate))
    if not ports:
        raise RuntimeError('Не удалось определить SSH-порт. Настройте доступ через консоль и повторите.')
    return sorted(p for p in ports if 0 < p < 65536)


def delete_ufw_rules(marker):
    if not shutil.which('ufw'):
        return
    if not ufw_active():
        # A failure may occur after adding rules but before enabling UFW.
        # Only remove rules that UFW itself reports with our unique comment.
        for line in output('ufw', 'show', 'added').splitlines():
            match = re.match(r"ufw allow (\d+/tcp) comment ['\"]?([^'\"]+)", line.strip())
            if match and match.group(2) == marker:
                run('ufw', '--force', 'delete', 'allow', match.group(1))
        return
    for _ in range(30):
        lines = output('ufw', 'status', 'numbered').splitlines()
        numbers = [int(m.group(1)) for line in lines
                   if marker in line and (m := re.match(r'\[\s*(\d+)\]', line.strip()))]
        if not numbers:
            break
        run('ufw', '--force', 'delete', str(max(numbers)), check=True)


def rollback(state):
    backup = Path(state['backup'])
    errors = []
    heading('ВОССТАНОВЛЕНИЕ')
    notice('Восстановление файлов и правил UFW из ' + str(backup))
    try:
        delete_ufw_rules(state['marker'])
        if not state.get('ufw_was_active', False) and shutil.which('ufw') and ufw_active():
            run('ufw', '--force', 'disable')
    except Exception as exc:
        errors.append('UFW: ' + str(exc))
    for rec in state['paths']:
        try:
            restore_path(rec, backup)
        except Exception as exc:
            errors.append(rec['path'] + ': ' + str(exc))
    if shutil.which('nginx'):
        if state['nginx_was_active']:
            p = run('nginx', '-t', check=False)
            if p.returncode == 0:
                if run('systemctl', 'restart', 'nginx', check=False).returncode:
                    errors.append('Не удалось перезапустить nginx')
            else:
                errors.append('Восстановленный nginx не прошёл проверку nginx -t')
        else:
            run('systemctl', 'stop', 'nginx', check=False)
        if not state['nginx_was_enabled']:
            run('systemctl', 'disable', 'nginx', check=False)
    # A unique cert-name is used; deletion is restricted to the certificate created by this run.
    if state.get('cert_created'):
        if run('certbot', 'delete', '--non-interactive', '--cert-name', state['cert_name'], check=False).returncode:
            errors.append('Не удалось удалить созданный сертификат; сохраните snapshot для ручного восстановления')
    if errors:
        notice('Не всё восстановлено:', 'error')
        print(*errors, sep='\n - ')
        print('Снимок сохранён; исправьте ошибки и повторите откат.')
        return False
    CURRENT.unlink(missing_ok=True)
    notice('Откат файлов и правил завершён. Установленные apt-пакеты и зависимости сохранены.', 'ok')
    return True


def nginx_config(domain, cert_name, modern, production):
    prefix = '/var/www/selfsteal'
    http = f'''server {{
    listen 0.0.0.0:80;
    server_name {domain};
    root {prefix};
    location ^~ /.well-known/acme-challenge/ {{ try_files $uri =404; }}
    location / {{ {'return 301 https://$host$request_uri;' if production else "return 200 'ok';"} }}
}}
'''
    if not production:
        return http
    http2 = '    http2 on;\n' if modern else ''
    listen = '    listen 127.0.0.1:8443 ssl;' + ('' if modern else ' # HTTP/2 configured below')
    if not modern:
        listen = '    listen 127.0.0.1:8443 ssl http2;'
    return http + f'''server {{
{listen}
{http2}    server_name {domain};
    ssl_certificate /etc/letsencrypt/live/{cert_name}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{cert_name}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    root {prefix};
    index index.html;
    error_page 404 /404.html;
    add_header X-Content-Type-Options nosniff always;
    location / {{ try_files $uri $uri/ =404; }}
    location = /404.html {{ internal; }}
}}
'''


def write_conf(contents):
    CONF.parent.mkdir(parents=True, exist_ok=True)
    CONF.write_text(contents, encoding='utf-8')
    ENABLED.parent.mkdir(parents=True, exist_ok=True)
    if ENABLED.is_symlink() or ENABLED.exists():
        remove_path(ENABLED)
    ENABLED.symlink_to(CONF)
    run('nginx', '-t')
    run('systemctl', 'reload', 'nginx')


def make_profile(domain):
    # OpenSSL emits RFC 8410 DER keys whose raw X25519 material is the final 32 bytes.
    with tempfile.TemporaryDirectory(prefix='selfsteal-keys-') as tmp:
        key = Path(tmp) / 'key.pem'
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'X25519', '-out', str(key)], check=True)
        private_der = subprocess.check_output(['openssl', 'pkey', '-in', str(key), '-outform', 'DER'])
        public_der = subprocess.check_output(['openssl', 'pkey', '-in', str(key), '-pubout', '-outform', 'DER'])
    private_prefix = bytes.fromhex('302e020100300506032b656e04220420')
    public_prefix = bytes.fromhex('302a300506032b656e032100')
    if not private_der.startswith(private_prefix) or len(private_der) != len(private_prefix) + 32:
        raise RuntimeError('Неподдерживаемый формат приватного ключа OpenSSL X25519')
    if not public_der.startswith(public_prefix) or len(public_der) != len(public_prefix) + 32:
        raise RuntimeError('Неподдерживаемый формат публичного ключа OpenSSL X25519')
    enc = lambda b: base64.urlsafe_b64encode(b).decode().rstrip('=')
    private, public = enc(private_der[-32:]), enc(public_der[-32:])
    shortid = os.urandom(8).hex()
    inbound = {'tag': 'VLESS_SELFSTEAL', 'listen': '0.0.0.0', 'port': 443,
               'protocol': 'vless', 'settings': {'clients': [], 'decryption': 'none'},
               'streamSettings': {'network': 'tcp', 'security': 'reality',
                   'realitySettings': {'show': False, 'dest': '127.0.0.1:8443', 'xver': 0,
                       'serverNames': [domain], 'privateKey': private, 'shortIds': [shortid]}},
               'sniffing': {'enabled': True, 'destOverride': ['http', 'tls', 'quic']}}
    atomic_json(PROFILE, inbound)
    return public, shortid


def render_site(domain, source=None, site=SITE):
    choices = sorted((ROOT / 'templates').glob('*/index.html'))
    if len(choices) != 20:
        raise RuntimeError(f'Ожидается 20 шаблонов; найдено {len(choices)}')
    source = source or random.SystemRandom().choice(choices)
    if source not in choices:
        raise ValueError('Неизвестный шаблон сайта')
    site.mkdir(parents=True, exist_ok=True)
    for filename in ('index.html', '404.html', 'favicon.svg'):
        shutil.copy2(source.parent / filename, site / filename)
    (site / 'robots.txt').write_text(f'User-agent: *\nAllow: /\nSitemap: https://{domain}/sitemap.xml\n', encoding='utf-8')
    (site / 'sitemap.xml').write_text(f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://{domain}/</loc></url></urlset>\n', encoding='utf-8')
    return source.parent.name


def change_site():
    heading('Смена сайта-заглушки')
    if not CURRENT.exists():
        raise RuntimeError('Активная установка не найдена. Сначала установите Selfsteal.')
    state = read_state()
    domain = state.get('domain', '')
    if not valid_domain(domain):
        raise RuntimeError('В состоянии установки не найден корректный домен.')
    if SITE.is_symlink() or not SITE.is_dir():
        raise RuntimeError('Каталог сайта отсутствует или является ссылкой; проверьте его вручную.')
    if not CONF.is_file() or not ENABLED.exists() or not status_service('nginx', 'is-active'):
        raise RuntimeError('Конфигурация Selfsteal не подключена или nginx не запущен.')
    choices = sorted((ROOT / 'templates').glob('*/index.html'))
    if len(choices) != 20:
        raise RuntimeError(f'Ожидается 20 шаблонов; найдено {len(choices)}')
    print(f'  Домен: {domain}')
    print('  Доступные варианты:')
    for number, source in enumerate(choices, 1):
        title = re.search(r'<title>(.*?)</title>', source.read_text(encoding='utf-8'), re.I | re.S)
        name = re.sub(r'\s+', ' ', title.group(1)).strip() if title else source.parent.name
        print(f'  {number:2}. {source.parent.name} — {name}')
    print('   0. Назад')
    selected = prompt('Номер сайта: ').strip()
    if selected == '0':
        return
    if not selected.isdigit() or not 1 <= int(selected) <= len(choices):
        raise ValueError('Выберите номер из списка или 0 для возврата.')
    source = choices[int(selected) - 1]
    if not confirm(f'Заменить сайт на {source.parent.name}?'):
        notice('Смена сайта отменена.')
        return
    run('nginx', '-t')
    # Keep the previous site on the same filesystem until the reload succeeds.
    temp = Path(tempfile.mkdtemp(prefix='.selfsteal-change-', dir=SITE.parent))
    candidate, previous = temp / 'new', temp / 'previous'
    completed = False
    try:
        shutil.copytree(SITE, candidate, symlinks=True)
        for name in ('index.html', '404.html', 'favicon.svg', 'robots.txt', 'sitemap.xml'):
            path = candidate / name
            if path.is_symlink() or path.is_dir():
                raise RuntimeError(f'Небезопасный файл в каталоге сайта: {name}')
        render_site(domain, source, candidate)
        os.rename(SITE, previous)
        try:
            os.rename(candidate, SITE)
            run('nginx', '-t')
            run('systemctl', 'reload', 'nginx')
            completed = True
        except BaseException:
            if SITE.exists():
                os.rename(SITE, candidate)
            os.rename(previous, SITE)
            run('systemctl', 'reload', 'nginx', check=False)
            raise
    finally:
        # Do not delete the original if restoring it failed.
        if completed or not previous.exists():
            shutil.rmtree(temp, ignore_errors=True)
        else:
            print('Предыдущий сайт сохранён в ' + str(previous), file=sys.stderr)
    notice('Сайт заменён: ' + source.parent.name, 'ok')
    notice('nginx перезагружен; robots.txt и sitemap.xml обновлены для ' + domain, 'ok')
    print('  Откройте https://' + domain + '/ и при необходимости обновите страницу без кэша.')


def check_report(domain, ip, public, shortid, template, snapshot):
    heading('РЕЗУЛЬТАТ УСТАНОВКИ')
    print('Домен:', domain, '| IP:', ip, '| Шаблон:', template)
    print('Снимок:', snapshot)
    print('JSON inbound (содержит приватный ключ):', PROFILE)
    print('Публичный ключ REALITY:', public)
    print('Short ID:', shortid)
    tests = [
        ('nginx config', ['nginx', '-t']),
        ('локальный HTTPS с проверкой сертификата', ['curl', '-fsS', '--noproxy', '*', '--resolve', f'{domain}:8443:127.0.0.1', f'https://{domain}:8443/', '-o', '/dev/null']),
        ('редирект HTTP', ['curl', '-fsS', '--noproxy', '*', '--resolve', f'{domain}:80:127.0.0.1', '-o', '/dev/null', '-w', '%{http_code}', f'http://{domain}/']),
        ('страница 404', ['curl', '-sS', '--noproxy', '*', '--resolve', f'{domain}:8443:127.0.0.1', '-o', '/dev/null', '-w', '%{http_code}', f'https://{domain}:8443/not-found-selfsteal-check']),
        ('robots.txt', ['curl', '-fsS', '--noproxy', '*', '--resolve', f'{domain}:8443:127.0.0.1', '-o', '/dev/null', f'https://{domain}:8443/robots.txt']),
        ('favicon.svg', ['curl', '-fsS', '--noproxy', '*', '--resolve', f'{domain}:8443:127.0.0.1', '-o', '/dev/null', f'https://{domain}:8443/favicon.svg']),
        ('срок сертификата более суток', ['openssl', 'x509', '-checkend', '86400', '-noout', '-in', f'/etc/letsencrypt/live/selfsteal-{domain}/fullchain.pem']),
    ]
    for name, cmd in tests:
        p = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        expected = {'редирект HTTP': '301', 'страница 404': '404'}.get(name)
        ok = p.returncode == 0 and (expected is None or p.stdout.strip() == expected)
        notice(name + (': ' + p.stderr.strip() if not ok and p.stderr.strip() else ''), 'ok' if ok else 'error')
    cert_name = 'selfsteal-' + domain
    renewal = subprocess.run(['certbot', 'renew', '--dry-run', '--cert-name', cert_name],
                             text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    notice('пробное продление сертификата', 'ok' if renewal.returncode == 0 else 'error')
    if renewal.returncode:
        print(renewal.stderr.strip())
    print('Далее вставьте inbound в Config Profile, примените профиль к ноде и настройте Host.')
    print('До применения профиля публичный HTTPS на 443 и работа VPN не считаются проверенными.')
    print('Внешняя проверка: curl -I https://' + domain + '/')
    print('Проверьте реальное подключение VPN-клиентом и доступность 8443 извне с другого устройства.')


def install():
    heading('УСТАНОВКА')
    if CURRENT.exists():
        raise RuntimeError('Обнаружена существующая установка/незавершённая операция. Сначала выполните откат.')
    if is_listening(8443):
        raise RuntimeError('Порт 8443 уже занят. Проверьте sudo ss -lntp и освободите порт перед установкой.')
    reuse_nginx = False
    if is_listening(80):
        ok, reason = inspect_default_nginx()
        if not ok:
            raise RuntimeError('Порт 80 занят; автоматическое продолжение недоступно: ' + reason + '. Проверьте sudo nginx -T и sudo ss -lntp.')
        notice('Порт 80 занят стандартной страницей nginx. Других сайтов в проверенной конфигурации не обнаружено.', 'warn')
        notice('Скрипт сохранит ссылку на стандартный сайт и состояние nginx, затем переключит конфигурацию. При откате они восстановятся.')
        if not confirm('Продолжить с установленным nginx?'):
            notice('Установка отменена; никаких изменений не внесено.', 'info')
            return
        reuse_nginx = True
    elif DEFAULT.exists() or DEFAULT.is_symlink():
        enabled_names = {p.name for p in DEFAULT.parent.iterdir()}
        conf_dir = Path('/etc/nginx/conf.d')
        if (not DEFAULT.is_symlink() or not DEFAULT.exists()
                or DEFAULT.resolve() != Path('/etc/nginx/sites-available/default').resolve()
                or enabled_names != {'default'}
                or (conf_dir.is_dir() and any(conf_dir.glob('*.conf')))
                or not shutil.which('dpkg-query') or not packaged_nginx_configs_match()
                or (shutil.which('nginx') and status_service('nginx', 'is-active'))):
            raise RuntimeError('Обнаружена существующая конфигурация nginx; её нельзя безопасно отключить автоматически. Проверьте sudo nginx -T.')
    if HOOK.exists() or HOOK.is_symlink() or CONF.exists() or CONF.is_symlink() or ENABLED.exists() or ENABLED.is_symlink():
        raise RuntimeError('Файлы selfsteal-installer уже существуют без записи об установке. Проверьте их вручную.')
    domain, ip, email = input_parameters()
    cert_name = 'selfsteal-' + domain
    if Path('/etc/letsencrypt/renewal', cert_name + '.conf').exists() or Path('/etc/letsencrypt/live', cert_name).exists():
        raise RuntimeError('Сертификат с этим именем уже существует; установщик не будет его перезаписывать.')
    if not confirm('Будут установлены пакеты, изменены nginx и UFW. Продолжить?'):
        notice('Установка отменена.', 'info')
        return
    if not (shutil.which('ufw') and ufw_active()):
        print('UFW сейчас не активен. Его включение может закрыть порты других сервисов. Текущие слушающие службы:')
        print(output('ss', '-lntup'))
        if not confirm('Правила SSH, 80 и 443 будут добавлены. Включить UFW?'):
            notice('Отменено до изменений.', 'info')
            return
    # Check again after user input, before creating a snapshot or changing services.
    if is_listening(8443):
        raise RuntimeError('За время подготовки порт 8443 был занят. Установка остановлена.')
    if is_listening(80):
        if not reuse_nginx:
            raise RuntimeError('За время подготовки порт 80 был занят. Установка остановлена.')
        ok, reason = inspect_default_nginx()
        if not ok:
            raise RuntimeError('Конфигурация nginx изменилась во время подготовки: ' + reason)
    elif reuse_nginx:
        raise RuntimeError('Состояние порта 80 изменилось во время подготовки. Запустите установку заново.')
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE, 0o700)
    stamp = dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + os.urandom(3).hex()
    backup = SNAPSHOTS / stamp
    backup.mkdir(parents=True, mode=0o700)
    marker = SSH_MARK + stamp
    paths = [save_path(p, backup, i) for i, p in enumerate([SITE, CONF, ENABLED, DEFAULT, HOOK, PROFILE])]
    state = {'backup': str(backup), 'paths': paths, 'marker': marker, 'domain': domain,
             'cert_name': cert_name, 'cert_created': False,
             'nginx_was_active': status_service('nginx', 'is-active'),
             'nginx_was_enabled': status_service('nginx', 'is-enabled'),
             'ufw_was_active': ufw_active() if shutil.which('ufw') else False}
    save_state(state)
    try:
        heading('ПАКЕТЫ И СЕТЬ')
        notice('Снимок сохранён: ' + str(backup), 'ok')
        run('apt-get', 'update')
        run('apt-get', 'install', '-y', 'nginx', 'certbot', 'ufw', 'openssl', 'curl')
        for port in sorted(set(ssh_ports() + [80, 443])):
            run('ufw', 'allow', f'{port}/tcp', 'comment', marker)
        if not ufw_active():
            run('ufw', '--force', 'enable')
        remove_path(SITE)
        SITE.mkdir(parents=True, exist_ok=True)
        # Only disable nginx's default site; the original symlink was saved above.
        remove_path(DEFAULT)
        run('systemctl', 'start', 'nginx')
        write_conf(nginx_config(domain, cert_name, False, False))
        heading('СЕРТИФИКАТ')
        run('certbot', 'certonly', '--webroot', '-w', str(SITE), '--cert-name', cert_name,
            '-d', domain, '--agree-tos', '--email', email, '--non-interactive')
        state['cert_created'] = True
        save_state(state)
        version = subprocess.run(['nginx', '-v'], text=True, capture_output=True, check=True).stderr
        match = re.search(r'nginx/(\d+)\.(\d+)\.(\d+)', version)
        if not match:
            raise RuntimeError('Не удалось определить версию nginx: ' + version)
        modern = tuple(map(int, match.groups())) >= (1, 25, 1)
        heading('САЙТ И REALITY')
        template = render_site(domain)
        write_conf(nginx_config(domain, cert_name, modern, True))
        HOOK.parent.mkdir(parents=True, exist_ok=True)
        HOOK.write_text('#!/bin/sh\nif [ "$RENEWED_LINEAGE" = "/etc/letsencrypt/live/' + cert_name + '" ]; then\n  nginx -t && systemctl reload nginx\nfi\n', encoding='utf-8')
        HOOK.chmod(0o755)
        public, shortid = make_profile(domain)
        check_report(domain, ip, public, shortid, template, backup)
    except BaseException:
        print('\nУстановка прервана, запускаю откат доступных изменений.', file=sys.stderr)
        rollback(state)
        raise


def main():
    if sys.version_info < (3, 9):
        sys.exit('Для установщика необходим Python 3.9 или новее.')
    if os.geteuid() != 0:
        sys.exit('Запустите от root: sudo bash install.sh')
    if not shutil.which('apt-get') or not shutil.which('systemctl') or not shutil.which('ss'):
        sys.exit('Поддерживаются серверы Ubuntu/Debian с apt, systemd и iproute2.')
    banner()
    while True:
        info()
        heading('ГЛАВНОЕ МЕНЮ')
        print(f'  {styled("[1]", "cyan", True)}  Установить Selfsteal')
        print(f'  {styled("[2]", "cyan", True)}  Откатить последнюю установку')
        print(f'  {styled("[3]", "cyan", True)}  Изменить сайт-заглушку')
        print(f'  {styled("[4]", "cyan", True)}  Выход из скрипта')
        try:
            choice = prompt('Выберите действие: ').strip().casefold()
            if choice == '1':
                install()
            elif choice == '2':
                if not CURRENT.exists():
                    notice('Активный снимок не найден.', 'warn')
                elif confirm('Откатить конфигурацию этого установщика?'):
                    rollback(read_state())
            elif choice == '3':
                change_site()
            elif choice in ('4', '0', 'q', 'quit', 'exit', 'выход'):
                notice('Скрипт завершён. Вы вернулись в командную строку.', 'ok')
                return
            else:
                notice('Выберите 1, 2, 3 или 4.', 'warn')
        except (EOFError, KeyboardInterrupt):
            print()
            notice('Скрипт завершён. Вы вернулись в командную строку.', 'ok')
            return
        except Exception as exc:
            print(styled('  [×] Ошибка: ', 'red', True) + str(exc), file=sys.stderr)


if __name__ == '__main__':
    main()
