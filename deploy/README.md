# Deployment — Azubi-Werkbank

Target: Ubuntu 24.04 LTS (Debian 12 works the same way), one server, reachable
from the internet under a domain such as `werkbank.example.de`. The app runs
as the unprivileged system user `werkbank` on `127.0.0.1:8000`; Caddy
terminates TLS in front of it and obtains the certificate automatically.

| Path | Content | Owner / mode |
|---|---|---|
| `/opt/werkbank` | code (git checkout) and `.venv` | root, read-only for the service |
| `/etc/werkbank/config.yaml` | configuration | `root:werkbank 0640` |
| `/var/lib/werkbank/users.yaml` | user list | `werkbank 0600` |
| `/var/lib/werkbank/data/` | `credentials.json`, `secret.key` | `werkbank 0700/0600` |
| `/var/lib/werkbank/progress/` | answers per user and workbook | `werkbank 0700/0600` |
| `/var/lib/werkbank/workbooks/` | catalogs + `assets/` | `werkbank:werkbank 2775` |

## Quick install with the script

After step 1 (DNS and ports), everything else can be done by
`deploy/install.sh`:

```sh
sudo git clone https://github.com/MajorMaxdom/azubi-werkbank.git /opt/werkbank
cd /opt/werkbank
sudo ./deploy/install.sh
```

It asks for the data directory (default `/var/lib/werkbank`), the domain, the
local port of the web server (default 8000; ports in use are rejected) and
the first Fachbetreuer, then installs packages, the system user, the venv,
the config, the systemd service, the `werkbank` command and — if a domain is
given — Caddy, and prints the invite link of the first Fachbetreuer.
Non-interactive:

```sh
sudo ./deploy/install.sh --yes --data-dir /var/lib/werkbank --port 8000 \
  --domain werkbank.firma.de --admin mmustermann --admin-name "Max Mustermann"
```

Options: `--tls-internal` (Caddy's own CA, for intranet/VPN-only setups),
`--no-caddy`, `--timezone`; `--help` lists all. Running it again is
safe: existing config, data and users are kept. Code and data must not live
below `/home`, `/root` or `/tmp` (the service is sandboxed). An existing Caddy
setup stays intact: the site goes to `/etc/caddy/werkbank.caddy` and the main
`Caddyfile` only gets an `import` line (backed up first, restored if the
combined config does not validate).

The following sections describe the same steps by hand.

## 1. DNS and ports

1. Create an `A` record (and `AAAA` if the server has IPv6) for your domain
   pointing to the server's public IP.
2. Forward TCP ports **80** and **443** from your router to the server.
   Port 80 is needed for the certificate challenge and the redirect to HTTPS.
3. Open them in the host firewall if one is active, e.g.
   `sudo ufw allow 80/tcp && sudo ufw allow 443/tcp`.
   Port 8000 must **not** be opened — the app only listens on localhost.

## 2. Packages

```sh
sudo apt update
sudo apt install -y git python3 python3-venv debian-keyring debian-archive-keyring apt-transport-https curl
# Caddy from the official repository (https://caddyserver.com/docs/install#debian-ubuntu-raspbian)
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

Python 3.11 or newer is required (`python3 --version`).

## 3. User and code

```sh
sudo useradd --system --home-dir /var/lib/werkbank --shell /usr/sbin/nologin werkbank
sudo git clone <repository-url> /opt/werkbank          # see below without a remote
sudo python3 -m venv /opt/werkbank/.venv
sudo /opt/werkbank/.venv/bin/pip install --upgrade pip
sudo /opt/werkbank/.venv/bin/pip install -e /opt/werkbank
```

Without a git remote, bundle the repository on the development machine and
clone from the bundle:

```sh
git -C /path/to/azubi-werkbank bundle create /tmp/werkbank.bundle --all   # dev machine
scp /tmp/werkbank.bundle <server>:/tmp/                                   # copy
sudo git clone /tmp/werkbank.bundle /opt/werkbank                         # server
```

The editable install (`-e`) is intended: templates, static files and
`locales/` are read from `/opt/werkbank`.

## 4. Configuration

```sh
sudo mkdir -p /etc/werkbank
sudo cp /opt/werkbank/config.example.yaml /etc/werkbank/config.yaml
sudo chown root:werkbank /etc/werkbank/config.yaml
sudo chmod 0640 /etc/werkbank/config.yaml
sudoedit /etc/werkbank/config.yaml
```

Set at least these keys (absolute paths, because the config file does not live
next to the data):

```yaml
base_url: https://werkbank.example.de
listen_host: 127.0.0.1
listen_port: 8000
paths:
  workbooks: /var/lib/werkbank/workbooks
  progress: /var/lib/werkbank/progress
  users: /var/lib/werkbank/users.yaml
  data: /var/lib/werkbank/data
  locales: /opt/werkbank/locales
secure_cookies: true
timezone: Europe/Berlin
```

`base_url` must be exactly the address users type in the browser (scheme and
host, no trailing path): it is used for invite links and the same-origin check
of every form and autosave request.

## 5. State directory and catalogs

```sh
sudo install -d -o werkbank -g werkbank -m 0750 /var/lib/werkbank
sudo install -d -o werkbank -g werkbank -m 2775 /var/lib/werkbank/workbooks /var/lib/werkbank/workbooks/assets
sudo cp /opt/werkbank/workbooks/linux-basics.yaml /var/lib/werkbank/workbooks/
sudo cp /opt/werkbank/workbooks/_template.yaml /var/lib/werkbank/workbooks/
sudo chown werkbank:werkbank /var/lib/werkbank/workbooks/*.yaml
sudo -u werkbank /opt/werkbank/.venv/bin/werkbank validate /var/lib/werkbank/workbooks
```

Catalog authors are added to the `werkbank` group
(`sudo usermod -aG werkbank <login>`, then log in again) and edit the files in
`/var/lib/werkbank/workbooks/` directly. The server picks up every change within
about a second; broken files keep their last valid version and the error is
shown on `/admin/catalogs`. See `docs/workbook-template.yaml` for all fields.
Fachbetreuer can also use the form editor in the browser (`/admin/editor`);
it writes to the same directory and keeps the last 10 versions of every file
in `/var/lib/werkbank/workbooks/_backups/`.

## 6. Service

```sh
sudo cp /opt/werkbank/deploy/werkbank.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now werkbank
systemctl status werkbank
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/login   # 200
```

Logs: `journalctl -u werkbank -f`.

## 7. Caddy

The site gets its own file, so other sites on the same Caddy stay untouched:

```sh
sudo cp /opt/werkbank/deploy/Caddyfile /etc/caddy/werkbank.caddy
sudoedit /etc/caddy/werkbank.caddy   # replace werkbank.example.de with your domain
sudo cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak
echo 'import /etc/caddy/werkbank.caddy' | sudo tee -a /etc/caddy/Caddyfile
sudo install -d -o caddy -g caddy /var/log/caddy
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Open `https://<your-domain>/` — you should see the login page with a valid
certificate.

## 8. First Fachbetreuer

```sh
sudo -u werkbank WERKBANK_CONFIG=/etc/werkbank/config.yaml \
  /opt/werkbank/.venv/bin/werkbank user add mmustermann --name "Max Mustermann" --role trainer
```

Open the printed invite link, set a password (at least 12 characters) and
create all further users in the browser under **Nutzer**. The same CLI offers
`werkbank user list` and `werkbank user reset <username>` (new invite link, all
sessions end). A convenient alias for admins:

```sh
alias werkbank='sudo -u werkbank WERKBANK_CONFIG=/etc/werkbank/config.yaml /opt/werkbank/.venv/bin/werkbank'
```

## 9. Backups

Back up `/var/lib/werkbank` (users, credentials, secret key, progress,
catalogs) and `/etc/werkbank`. Files are written atomically, so copying them
while the service runs is safe. Two simple options:

**restic** (encrypted, deduplicated, e.g. to a NAS or S3):

```sh
sudo apt install -y restic
sudo restic -r /mnt/backup/werkbank init
# /etc/cron.d/werkbank-backup
15 2 * * * root restic -r /mnt/backup/werkbank --password-file /root/.restic-pw backup /var/lib/werkbank /etc/werkbank && restic -r /mnt/backup/werkbank --password-file /root/.restic-pw forget --keep-daily 14 --keep-weekly 8 --prune
```

**Nightly git commit** (history of every answer; keep the repository private,
it contains password hashes):

```sh
sudo -u werkbank git -C /var/lib/werkbank init
# /etc/cron.d/werkbank-git
30 2 * * * werkbank cd /var/lib/werkbank && git add -A && git commit -qm "nightly $(date -I)" || true
```

Restore = stop the service, put the files back (keep owner `werkbank` and the
modes from the table above), start the service.

## 10. Updates

```sh
cd /opt/werkbank
sudo git pull
sudo /opt/werkbank/.venv/bin/pip install -e /opt/werkbank
sudo systemctl restart werkbank
```

Read `CHANGELOG.md` before updating. Catalog and user changes never need a
restart.

## 11. Optional: fail2ban

The app logs every failed login as one line
`auth.login_failed user=<name> ip=<ip>` (the IP comes from Caddy's
`X-Forwarded-For`). The app itself already locks a user after 5 failures and
limits 20 failures per IP in 15 minutes; fail2ban adds a firewall ban:

```sh
sudo apt install -y fail2ban
sudo cp /opt/werkbank/deploy/fail2ban-filter.conf /etc/fail2ban/filter.d/werkbank.conf
sudo cp /opt/werkbank/deploy/fail2ban-jail.conf /etc/fail2ban/jail.d/werkbank.conf
sudo systemctl restart fail2ban
sudo fail2ban-client status werkbank
```

## Uninstall

```sh
cd /opt/werkbank
sudo ./deploy/uninstall.sh                     # remove everything, KEEP the data
sudo ./deploy/uninstall.sh --backup /root/werkbank-backup.tar.gz --purge-data
```

Removes the service, the `werkbank` command, `/etc/werkbank`, the Caddy site
`/etc/caddy/werkbank.caddy` and its `import` line (the main Caddyfile is
backed up and validated; other sites and Caddy itself stay), the fail2ban
filter/jail, `/etc/cron.d/werkbank-*`, the venv and the system user.

The data directory (users, answers, workbooks) is only deleted with
`--purge-data`; `--backup FILE` writes a `.tar.gz` of data and config first.
If the data was kept in an earlier run, delete it later with
`--data-dir DIR --purge-data`. `--remove-code` also deletes the clone itself,
`--yes` skips the questions. Packages (Python, Caddy, fail2ban) are not
uninstalled.

## Troubleshooting

- **Login or saving fails with "Die Sitzung ist abgelaufen oder die Anfrage kam
  von einer fremden Seite"** — `base_url` does not match the address in the
  browser (scheme, host or port), or the Caddy site still sends
  `Referrer-Policy no-referrer` (installs before 1.10.1): with it, browsers
  send `Origin: null` on form submissions. Use `same-origin` in
  `/etc/caddy/werkbank.caddy` and `systemctl reload caddy`, or run
  `deploy/install.sh` again after `git pull`.
- **Installer hangs at "Import line added" / Caddy rejects the site with
  `open /var/log/caddy/werkbank.log: permission denied`** (installs before
  1.10.2): `sudo chown caddy:caddy /var/log/caddy/werkbank.log`, then
  `sudo caddy reload --config /etc/caddy/Caddyfile`.
- **Login always fails over plain `http://`** — expected with
  `secure_cookies: true`; always use the HTTPS address via Caddy.
- **`/admin/catalogs` shows errors** — fix the file; the previous valid version
  stays online meanwhile.
- **Service does not start** — `journalctl -u werkbank -e`; check that
  `/etc/werkbank/config.yaml` is readable by the group `werkbank` and that the
  paths exist.
