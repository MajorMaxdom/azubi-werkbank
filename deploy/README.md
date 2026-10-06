# Deployment

Target: Ubuntu 24.04 LTS (Debian 12 works the same way), one server, reachable
from the internet under a domain such as `arbeitsheft.example.de`. The app runs
as the unprivileged system user `workbook` on `127.0.0.1:8000`; Caddy
terminates TLS in front of it and obtains the certificate automatically.

| Path | Content | Owner / mode |
|---|---|---|
| `/opt/workbook` | code (git checkout) and `.venv` | root, read-only for the service |
| `/etc/workbook/config.yaml` | configuration | `root:workbook 0640` |
| `/var/lib/workbook/users.yaml` | user list | `workbook 0600` |
| `/var/lib/workbook/data/` | `credentials.json`, `secret.key` | `workbook 0700/0600` |
| `/var/lib/workbook/progress/` | answers per user and workbook | `workbook 0700/0600` |
| `/var/lib/workbook/workbooks/` | catalogs + `assets/` | `workbook:workbook 2775` |

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
sudo useradd --system --home-dir /var/lib/workbook --shell /usr/sbin/nologin workbook
sudo git clone <repository-url> /opt/workbook          # or copy the project there
sudo python3 -m venv /opt/workbook/.venv
sudo /opt/workbook/.venv/bin/pip install --upgrade pip
sudo /opt/workbook/.venv/bin/pip install -e /opt/workbook
```

The editable install (`-e`) is intended: templates, static files and
`locales/` are read from `/opt/workbook`.

## 4. Configuration

```sh
sudo mkdir -p /etc/workbook
sudo cp /opt/workbook/config.example.yaml /etc/workbook/config.yaml
sudo chown root:workbook /etc/workbook/config.yaml
sudo chmod 0640 /etc/workbook/config.yaml
sudoedit /etc/workbook/config.yaml
```

Set at least these keys (absolute paths, because the config file does not live
next to the data):

```yaml
base_url: https://arbeitsheft.example.de
listen_host: 127.0.0.1
listen_port: 8000
paths:
  workbooks: /var/lib/workbook/workbooks
  progress: /var/lib/workbook/progress
  users: /var/lib/workbook/users.yaml
  data: /var/lib/workbook/data
  locales: /opt/workbook/locales
secure_cookies: true
```

`base_url` must be exactly the address users type in the browser (scheme and
host, no trailing path): it is used for invite links and the same-origin check
of every form and autosave request.

## 5. State directory and catalogs

```sh
sudo install -d -o workbook -g workbook -m 0750 /var/lib/workbook
sudo install -d -o workbook -g workbook -m 2775 /var/lib/workbook/workbooks /var/lib/workbook/workbooks/assets
sudo cp /opt/workbook/workbooks/network-security.yaml /var/lib/workbook/workbooks/
sudo cp /opt/workbook/workbooks/_template.yaml /var/lib/workbook/workbooks/
sudo chown workbook:workbook /var/lib/workbook/workbooks/*.yaml
sudo -u workbook /opt/workbook/.venv/bin/workbook validate /var/lib/workbook/workbooks
```

Catalog authors are added to the `workbook` group
(`sudo usermod -aG workbook <login>`, then log in again) and edit the files in
`/var/lib/workbook/workbooks/` directly. The server picks up every change within
about a second; broken files keep their last valid version and the error is
shown on `/admin/catalogs`. See `docs/workbook-template.yaml` for all fields.

## 6. Service

```sh
sudo cp /opt/workbook/deploy/workbook.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now workbook
systemctl status workbook
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/login   # 200
```

Logs: `journalctl -u workbook -f`.

## 7. Caddy

```sh
sudo cp /opt/workbook/deploy/Caddyfile /etc/caddy/Caddyfile
sudoedit /etc/caddy/Caddyfile        # replace arbeitsheft.example.de with your domain
sudo install -d -o caddy -g caddy /var/log/caddy
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Open `https://<your-domain>/` — you should see the login page with a valid
certificate.

## 8. First Fachbetreuer

```sh
sudo -u workbook WORKBOOK_CONFIG=/etc/workbook/config.yaml \
  /opt/workbook/.venv/bin/workbook user add mmustermann --name "Max Mustermann" --role trainer
```

Open the printed invite link, set a password (at least 12 characters) and
create all further users in the browser under **Nutzer**. The same CLI offers
`workbook user list` and `workbook user reset <username>` (new invite link, all
sessions end). A convenient alias for admins:

```sh
alias workbook='sudo -u workbook WORKBOOK_CONFIG=/etc/workbook/config.yaml /opt/workbook/.venv/bin/workbook'
```

## 9. Backups

Back up `/var/lib/workbook` (users, credentials, secret key, progress,
catalogs) and `/etc/workbook`. Files are written atomically, so copying them
while the service runs is safe. Two simple options:

**restic** (encrypted, deduplicated, e.g. to a NAS or S3):

```sh
sudo restic -r /mnt/backup/workbook init
# /etc/cron.d/workbook-backup
15 2 * * * root restic -r /mnt/backup/workbook --password-file /root/.restic-pw backup /var/lib/workbook /etc/workbook && restic -r /mnt/backup/workbook --password-file /root/.restic-pw forget --keep-daily 14 --keep-weekly 8 --prune
```

**Nightly git commit** (history of every answer; keep the repository private,
it contains password hashes):

```sh
sudo -u workbook git -C /var/lib/workbook init
# /etc/cron.d/workbook-git
30 2 * * * workbook cd /var/lib/workbook && git add -A && git commit -qm "nightly $(date -I)" || true
```

Restore = stop the service, put the files back (keep owner `workbook` and the
modes from the table above), start the service.

## 10. Updates

```sh
cd /opt/workbook
sudo git pull
sudo /opt/workbook/.venv/bin/pip install -e /opt/workbook
sudo systemctl restart workbook
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
sudo cp /opt/workbook/deploy/fail2ban-filter.conf /etc/fail2ban/filter.d/workbook.conf
sudo cp /opt/workbook/deploy/fail2ban-jail.conf /etc/fail2ban/jail.d/workbook.conf
sudo systemctl restart fail2ban
sudo fail2ban-client status workbook
```

## Troubleshooting

- **Login or saving fails with "Die Sitzung ist abgelaufen oder die Anfrage kam
  von einer fremden Seite"** — `base_url` does not match the address in the
  browser (scheme, host or port).
- **Login always fails over plain `http://`** — expected with
  `secure_cookies: true`; always use the HTTPS address via Caddy.
- **`/admin/catalogs` shows errors** — fix the file; the previous valid version
  stays online meanwhile.
- **Service does not start** — `journalctl -u workbook -e`; check that
  `/etc/workbook/config.yaml` is readable by the group `workbook` and that the
  paths exist.
