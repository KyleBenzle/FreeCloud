# KyleBenzle.com + FreeCloud Raspberry Pi Recovery Guide

Use this to rebuild the current working server after a complete Pi wipe.

## 1. Working architecture

``` text
Internet
   |
Breezeline/Sagemcom router
Public IPv4 observed during setup: 65.60.254.191
   |
DMZ -> 192.168.40.3
   |
TP-Link Archer C7
   |
Raspberry Pi: 192.168.1.114
   |
   +-- nginx
   +-- PHP 8.4-FPM
   +-- MariaDB
   +-- kylebenzle.com
   +-- /FreeCloud
   |
External HDD /dev/sda1
   |
/mnt/freecloud-drive
   |
/mnt/freecloud-drive/FreeCloud/Audiobooks
```

The Sagemcom DMZ target is the TP-Link WAN address: `192.168.40.3`.

The public IPv4 can change. DNS for `kylebenzle.com` and
`www.kylebenzle.com` must point to the current public IPv4.

## 2. Main filesystem locations

Personal website: `/var/www/kylebenzle.com/public/`

FreeCloud server: `/var/www/kylebenzle.com/public/FreeCloud/`

Normal files physically stored by the FreeCloud web server:
`/var/www/kylebenzle.com/public/FreeCloud/freecloud_files/`

FreeCloud Pi client source: `/home/kbpi/FreeCloud/`

Pi FreeCloud configuration: `/home/kbpi/.config/freecloud/device.json`

External HDD: `/mnt/freecloud-drive/`

Audiobooks: `/mnt/freecloud-drive/FreeCloud/Audiobooks/`

nginx site configuration: `/etc/nginx/sites-available/kylebenzle.com`

## 3. Install server software

``` bash
sudo apt update
sudo apt install -y nginx mariadb-server php8.4-fpm php8.4-mysql certbot python3-certbot-nginx

sudo systemctl enable nginx
sudo systemctl enable php8.4-fpm
sudo systemctl enable mariadb
```

Verify:

``` bash
systemctl is-active nginx
systemctl is-active php8.4-fpm
systemctl is-active mariadb
```

All should return `active`.

## 4. Deploy personal website

``` bash
sudo mkdir -p /var/www/kylebenzle.com/public
sudo chown -R kbpi:kbpi /var/www/kylebenzle.com
```

Copy the website into: `/var/www/kylebenzle.com/public/`

Test locally:

``` bash
curl -I -H 'Host: kylebenzle.com' http://127.0.0.1/
```

## 5. nginx configuration

Important settings for the HTTPS server:

``` nginx
server {
    server_name kylebenzle.com www.kylebenzle.com;

    root /var/www/kylebenzle.com/public;
    index index.php index.html;

    client_max_body_size 10M;

    location ~* \.(bak|old|orig|save|swp|sql)$ { return 404; }
    location ~ ~$ { return 404; }
    location ~ /\. { return 404; }

    location / {
        try_files $uri $uri/ =404;
    }

    location ~ ^/FreeCloud/(freecloud_(files|sessions|meta)(/|$)|config\.json$) {
        deny all;
    }

    location ~ \.php$ {
        include snippets/fastcgi-php.conf;
        fastcgi_pass unix:/run/php/php8.4-fpm.sock;
    }

    listen 443 ssl;
    listen [::]:443 ssl ipv6only=on;

    ssl_certificate /etc/letsencrypt/live/kylebenzle.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/kylebenzle.com/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;
}
```

The important FreeCloud setting is:

``` nginx
client_max_body_size 10M;
```

Without it, publishing the audiobook metadata produced HTTP 413.

After nginx changes:

``` bash
sudo nginx -t
sudo systemctl reload nginx
```

## 6. HTTPS

After DNS and external routing work:

``` bash
sudo certbot --nginx -d kylebenzle.com -d www.kylebenzle.com
```

Certificates: `/etc/letsencrypt/live/kylebenzle.com/`

Verify:

``` bash
curl -I https://kylebenzle.com/
```

## 7. LAN / hairpin-NAT workaround

The router setup does not properly support accessing the public domain
from inside the LAN.

On the Pi, `/etc/hosts` needs:

``` text
127.0.0.1 kylebenzle.com www.kylebenzle.com
```

On the Linux laptop:

``` text
192.168.1.114 kylebenzle.com www.kylebenzle.com
```

Verify:

``` bash
getent hosts kylebenzle.com
curl -I https://kylebenzle.com/FreeCloud/
```

## 8. MariaDB book database

Database: `books` Database user: `kyle_user`

The database password must match the PHP configuration. Do not store the
password in this README; retrieve or reset it during recovery.

CSV source: `/var/www/kylebenzle.com/public/TitleList.csv`

Known imported count during setup: 1464 books.

Verify:

``` bash
mariadb -u kyle_user -p books -e 'SELECT COUNT(*) AS books_imported FROM books;'
```

Test PHP:

``` bash
curl -sS -o /dev/null -w "HTTP %{http_code}\n" \
  --resolve kylebenzle.com:443:127.0.0.1 \
  https://kylebenzle.com/bookreviews.php
```

Expected: `HTTP 200`

## 9. Deploy FreeCloud server

Source server directory: `FreeCloud/server/`

Deploy its contents into: `/var/www/kylebenzle.com/public/FreeCloud/`

Important files include:

``` text
index.php
freecloud.php
freecloud_api.php
freecloud_download.php
freecloud_preview.php
freecloud_transfer.php
favicon.ico
icon.png
logo.png
```

FreeCloud creates runtime directories such as:

``` text
freecloud_files
freecloud_sessions
freecloud_meta
```

nginx, not Apache, serves this installation. `.htaccess` alone therefore
does not protect private runtime data; keep the nginx deny rule.

Verify:

``` bash
curl -I https://kylebenzle.com/FreeCloud/
```

Expected: `HTTP/1.1 200 OK`

## 10. External HDD

Current filesystem: `/dev/sda1` (ext4)

Mount point: `/mnt/freecloud-drive`

UUID: `8cf36839-84a7-4e17-ae0e-3ee8f416352b`

Working `/etc/fstab` entry:

``` fstab
UUID=8cf36839-84a7-4e17-ae0e-3ee8f416352b /mnt/freecloud-drive ext4 defaults,noatime,nofail,x-systemd.device-timeout=15 0 2
```

Verify:

``` bash
findmnt /mnt/freecloud-drive
```

Expected source: `/dev/sda1`

## 11. Raspberry Pi FreeCloud storage node

Client: `/home/kbpi/FreeCloud/freecloud_cli.py`

Register a rebuilt Pi:

``` bash
cd /home/kbpi/FreeCloud

python3 freecloud_cli.py node setup \
  --domain https://kylebenzle.com/FreeCloud \
  --name "Raspberry Pi" \
  --type raspberry_pi
```

Enter the current FreeCloud password.

Current device ID: `5f45b034e80f424a961b64626a5520bd`

A complete rebuild may create a different ID unless explicitly reusing
this one.

## 12. Audiobooks storage node

Physical library: `/mnt/freecloud-drive/FreeCloud/Audiobooks`

Remote namespace: `/Audiobooks`

Mode: `storage_only`

Rule:

``` text
/mnt/freecloud-drive/FreeCloud/Audiobooks
    -> /Audiobooks
    [storage_only]
```

Check:

``` bash
cd /home/kbpi/FreeCloud
python3 freecloud_cli.py node status
```

Working configuration published 26,317 metadata entries and
approximately 496 direct children under `/Audiobooks`.

Publish:

``` bash
python3 freecloud_cli.py node publish
```

The exact count can change as the library changes.

`storage_only` means the actual audiobook files remain on the external
HDD. FreeCloud publishes their metadata/location rather than copying the
entire library into web-server storage.

## 13. FreeCloud Pi autostart

Enable:

``` bash
cd /home/kbpi/FreeCloud
python3 freecloud_cli.py node autostart enable
```

Check:

``` bash
python3 freecloud_cli.py node autostart status
```

Service file: `/home/kbpi/.config/systemd/user/freecloud-node.service`

Expected: `Enabled here.`

## 14. Linux desktop client

Linux source: `~/MEGA/FreeCloud/`

Configured local drive: `/home/lll/FreeCloud`

Server: `https://kylebenzle.com/FreeCloud`

Working UI dynamically displays:

``` text
This Computer | Raspberry Pi | Web Server
```

`Audiobooks` should be checked only under `Raspberry Pi`.

The Linux client's saved FreeCloud password must match the current
server password. An obsolete password causes API requests to return
`401 Unauthorized` and leaves stale device/metadata information.

## 15. Important Linux desktop fixes

The September 27, 2026 Linux source was fixed so that:

-   root/folder views merge local filesystem entries with server/device
    metadata;
-   local-only entries remain visible;
-   shared paths preserve all device/provider locations;
-   registered-device columns are dynamic;
-   cached folder data renders immediately;
-   every navigation also performs a live metadata refresh;
-   `/Audiobooks` correctly retrieves its Pi metadata;
-   a background exception callback `exc` closure/NameError bug was
    fixed.

Regression state after the fix:

``` text
27 tests passed
Python compilation passed
git diff --check passed
root union: 30 entries
/Audiobooks: 496 live children
```

Preserve/commit these changes before rebuilding from an older FreeCloud
source tree.

## 16. Router configuration

Breezeline/Sagemcom:

``` text
DMZ enabled
DMZ host: 192.168.40.3
Device: Archer_C7
```

The TP-Link Archer C7 then handles forwarding toward the Pi.

Required Internet services:

``` text
TCP 80  -> Raspberry Pi
TCP 443 -> Raspberry Pi
```

Pi LAN address: `192.168.1.114`

If router addresses change after a reset, update the DMZ/forwarding
configuration.

## 17. Fast health check

Run on the Pi:

``` bash
echo "=== SERVICES ==="
systemctl is-active nginx
systemctl is-active php8.4-fpm
systemctl is-active mariadb

echo "=== HDD ==="
findmnt /mnt/freecloud-drive

echo "=== FREECLOUD NODE ==="
cd /home/kbpi/FreeCloud
python3 freecloud_cli.py node status
python3 freecloud_cli.py node autostart status

echo "=== HTTPS ==="
curl -sS -o /dev/null -w "Homepage: HTTP %{http_code}\n" https://kylebenzle.com/
curl -sS -o /dev/null -w "Books: HTTP %{http_code}\n" https://kylebenzle.com/bookreviews.php
curl -sS -o /dev/null -w "FreeCloud: HTTP %{http_code}\n" https://kylebenzle.com/FreeCloud/
```

Healthy result:

``` text
active
active
active

/dev/sda1 mounted at /mnt/freecloud-drive

Raspberry Pi registered
Audiobooks storage_only rule present
Autostart enabled

Homepage: HTTP 200
Books: HTTP 200
FreeCloud: HTTP 200
```

## 18. Files that must be backed up

At minimum:

``` text
/var/www/kylebenzle.com/
/etc/nginx/sites-available/kylebenzle.com
/home/kbpi/FreeCloud/
/home/kbpi/.config/freecloud/
/etc/fstab
```

Also preserve separately:

-   MariaDB `books` database dump
-   `TitleList.csv`
-   current FreeCloud source repository, including the September 27
    Linux fixes
-   FreeCloud server configuration/runtime metadata required to
    reconstruct users/devices
-   this README

Do not make the Pi the only location containing this README or its own
recovery files.
