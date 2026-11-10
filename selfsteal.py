#!/usr/bin/env python3
"""Install a local HTTPS REALITY target without changing the Remnawave panel."""
import base64
import datetime as dt
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
import urllib.request
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


def run(*args, check=True, capture=False, input_text=None):
    print('+', ' '.join(str(x) for x in args), flush=True)
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


def is_listening(port):
    p = subprocess.run(['ss', '-H', '-lnt'], text=True, capture_output=True, check=True)
    return any(row.split()[3].rsplit(':', 1)[-1] == str(port)
               for row in p.stdout.splitlines() if len(row.split()) >= 4)


def info():
    print('\n=== Информация о сервере ===')
    print('Узел:', socket.gethostname())
    print('ОС:', output('bash', '-c', '. /etc/os-release; printf "%s" "$PRETTY_NAME"'))
    print('Ядро:', output('uname', '-r'))
    print('IPv4:', output('hostname', '-I') or 'не определён')
    print('Порты 80/443/8443:')
    print(output('ss', '-lntp') or 'нет данных')
    print('nginx:', output('nginx', '-v') or ('установлен' if shutil.which('nginx') else 'не установлен'))
    print('Снимок:', str(CURRENT) if CURRENT.exists() else 'нет активной установки')


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
    domain = input('Домен (A-запись на этот сервер): ').strip().lower().rstrip('.')
    if not valid_domain(domain):
        raise ValueError('Некорректный домен. Используйте полное имя в ASCII, например node.example.com.')
    suggested = server_ip()
    raw_ip = input(f'Публичный IPv4 сервера [{suggested}]: ').strip() or suggested
    ip = str(ipaddress.IPv4Address(raw_ip))
    email = input('Email для Let\'s Encrypt: ').strip()
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
    print('\nВосстановление файлов и правил UFW из', backup)
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
        print('Не всё восстановлено:', *errors, sep='\n - ')
        print('Снимок сохранён; исправьте ошибки и повторите откат.')
        return False
    CURRENT.unlink(missing_ok=True)
    print('Откат файлов и правил завершён. Установленные apt-пакеты и зависимости сохранены.')
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


def render_site(domain):
    choices = sorted((ROOT / 'templates').glob('*/index.html'))
    if len(choices) != 20:
        raise RuntimeError(f'Ожидается 20 шаблонов; найдено {len(choices)}')
    source = random.SystemRandom().choice(choices)
    SITE.mkdir(parents=True, exist_ok=True)
    for filename in ('index.html', '404.html', 'favicon.svg'):
        shutil.copy2(source.parent / filename, SITE / filename)
    (SITE / 'robots.txt').write_text(f'User-agent: *\nAllow: /\nSitemap: https://{domain}/sitemap.xml\n', encoding='utf-8')
    (SITE / 'sitemap.xml').write_text(f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://{domain}/</loc></url></urlset>\n', encoding='utf-8')
    return source.parent.name


def check_report(domain, ip, public, shortid, template, snapshot):
    print('\n=== Результат ===')
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
        print(('OK' if ok else 'ОШИБКА'), name, (p.stderr.strip() if not ok else ''))
    cert_name = 'selfsteal-' + domain
    renewal = subprocess.run(['certbot', 'renew', '--dry-run', '--cert-name', cert_name],
                             text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    print('OK' if renewal.returncode == 0 else 'ОШИБКА', 'пробное продление сертификата')
    if renewal.returncode:
        print(renewal.stderr.strip())
    print('Далее вставьте inbound в Config Profile, примените профиль к ноде и настройте Host.')
    print('До применения профиля публичный HTTPS на 443 и работа VPN не считаются проверенными.')
    print('Внешняя проверка: curl -I https://' + domain + '/')
    print('Проверьте реальное подключение VPN-клиентом и доступность 8443 извне с другого устройства.')


def install():
    if CURRENT.exists():
        raise RuntimeError('Обнаружена существующая установка/незавершённая операция. Сначала выполните откат.')
    if is_listening(80) or is_listening(8443):
        raise RuntimeError('Порт 80 или 8443 уже занят. Изучите ss -lntp и освободите порт перед установкой.')
    if HOOK.exists() or HOOK.is_symlink() or CONF.exists() or CONF.is_symlink() or ENABLED.exists() or ENABLED.is_symlink():
        raise RuntimeError('Файлы selfsteal-installer уже существуют без записи об установке. Проверьте их вручную.')
    domain, ip, email = input_parameters()
    cert_name = 'selfsteal-' + domain
    if Path('/etc/letsencrypt/renewal', cert_name + '.conf').exists() or Path('/etc/letsencrypt/live', cert_name).exists():
        raise RuntimeError('Сертификат с этим именем уже существует; установщик не будет его перезаписывать.')
    if input('Будут установлены пакеты, изменены nginx и UFW. Продолжить? [yes/no]: ').strip().lower() != 'yes':
        print('Отменено.')
        return
    if not (shutil.which('ufw') and ufw_active()):
        print('UFW сейчас не активен. Его включение может закрыть порты других сервисов. Текущие слушающие службы:')
        print(output('ss', '-lntup'))
        if input('Правила SSH, 80 и 443 будут добавлены. Включить UFW? [yes/no]: ').strip().lower() != 'yes':
            print('Отменено до изменений.')
            return
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
        run('certbot', 'certonly', '--webroot', '-w', str(SITE), '--cert-name', cert_name,
            '-d', domain, '--agree-tos', '--email', email, '--non-interactive')
        state['cert_created'] = True
        save_state(state)
        version = subprocess.run(['nginx', '-v'], text=True, capture_output=True, check=True).stderr
        match = re.search(r'nginx/(\d+)\.(\d+)\.(\d+)', version)
        if not match:
            raise RuntimeError('Не удалось определить версию nginx: ' + version)
        modern = tuple(map(int, match.groups())) >= (1, 25, 1)
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
    if os.geteuid() != 0:
        sys.exit('Запустите от root: sudo bash install.sh')
    if not shutil.which('apt-get') or not shutil.which('systemctl') or not shutil.which('ss'):
        sys.exit('Поддерживаются серверы Ubuntu/Debian с apt, systemd и iproute2.')
    while True:
        info()
        print('\n1. Установить\n2. Откатить последнюю установку\n3. Выход')
        try:
            choice = input('Выбор: ').strip()
            if choice == '1':
                install()
            elif choice == '2':
                if not CURRENT.exists():
                    print('Активный снимок не найден.')
                elif input('Откатить конфигурацию этого установщика? [yes/no]: ').strip().lower() == 'yes':
                    rollback(read_state())
            elif choice == '3':
                return
            else:
                print('Выберите 1, 2 или 3.')
        except (EOFError, KeyboardInterrupt):
            print('\nВыход.')
            return
        except Exception as exc:
            print('Ошибка:', exc, file=sys.stderr)


if __name__ == '__main__':
    main()
