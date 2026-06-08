# Debian and Ubuntu Submission

FreeCloud's upstream release is `2.1.1`. Debian repacks generated binaries out
of the source archive, so its first package version is `2.1.1+dfsg-1`.

## 1. Publish the upstream release

1. Commit the release files.
2. Push the `main` branch to GitHub.
3. Create and push the `v2.1.1` tag.
4. Create a GitHub release named `FreeCloud 2.1.1`.

Do not publish credentials, local configuration files, or signing keys.

## 2. File the Debian ITP

Send `debian-wnpp-itp.txt` as a plain-text email to:

```text
submit@bugs.debian.org
```

Send it from `Kyle Benzle <kbe@gmx.us>`. Debian will reply with a bug number.

Update `debian/changelog` after receiving the number:

```text
  * Initial Debian release. (Closes: #1234567)
```

Replace `1234567` with the real ITP number, then rebuild the package.

## 3. Sign the package

Create or select an OpenPGP signing key associated with `kbe@gmx.us`. Build a
signed source package:

```sh
debuild -S -sa
```

The first upload must include the orig source archive. Never upload a private
key; only the public key may be published.

## 4. Request Debian sponsorship

1. Create or sign in to an account at `https://mentors.debian.net/`.
2. Follow the site's upload instructions for the signed package.
3. File an RFS bug using `reportbug sponsorship-requests`.
4. Include the ITP number, mentors package URL, GitHub URL, license, and a
   short note that this is the first upload.

A Debian Developer must review and sponsor the first upload. Once accepted
into Debian unstable, Ubuntu can normally synchronize the package.

## 5. File the Ubuntu request

After filing the Debian ITP, create a new Ubuntu bug in Launchpad using the
contents of `ubuntu-needs-packaging.txt`. Add the Debian ITP URL where marked
and apply the `needs-packaging` tag.

The Debian path should remain primary. The Launchpad bug records Ubuntu
interest while Debian review and sponsorship proceed.
