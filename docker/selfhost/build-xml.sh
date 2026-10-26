#!/usr/bin/env bash
set -euo pipefail

# Release inputs: GNOME's published SHA256 files and GitHub's XMLSec release digest.
# Rebuild all three libraries together: distribution libxslt must not pull an old
# libxml2 SONAME into the same Python process as the new lxml/xmlsec bindings.
prefix=/opt/posthog-native
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cd "$work"

fetch() {
    local filename=$1 checksum=$2 url=$3
    curl --fail --location --proto '=https' --proto-redir '=https' --tlsv1.2 \
        --retry 3 --connect-timeout 20 --max-time 300 --output "$filename" "$url"
    printf '%s  %s\n' "$checksum" "$filename" | sha256sum --check --strict -
    tar --extract --file "$filename"
}

export PATH="$prefix/bin:$PATH"
export PKG_CONFIG_PATH="$prefix/lib/pkgconfig"
export CFLAGS='-O2 -fstack-protector-strong -D_FORTIFY_SOURCE=2'
export LDFLAGS="-Wl,-z,relro,-z,now -Wl,-rpath,$prefix/lib"

fetch libxml2-2.15.4.tar.xz \
    98087fd181d9070724f3fbc65c7377db03038eb92bd882374daff44940138821 \
    https://download.gnome.org/sources/libxml2/2.15/libxml2-2.15.4.tar.xz
(
    cd libxml2-2.15.4
    ./configure --prefix="$prefix" --disable-static --without-python --with-zlib --with-legacy
    make -j2
    make install
)

fetch libxslt-1.1.45.tar.xz \
    9acfe68419c4d06a45c550321b3212762d92f41465062ca4ea19e632ee5d216e \
    https://download.gnome.org/sources/libxslt/1.1/libxslt-1.1.45.tar.xz
(
    cd libxslt-1.1.45
    ./configure --prefix="$prefix" --disable-static --without-python --with-libxml-prefix="$prefix"
    make -j2
    make install
)

fetch xmlsec1-1.3.12.tar.gz \
    24045199af12d93fe5fdbbbf7e386e823e4842071e9432e2b90ac108b889a923 \
    https://github.com/lsh123/xmlsec/releases/download/1.3.12/xmlsec1-1.3.12.tar.gz
(
    cd xmlsec1-1.3.12
    ./configure --prefix="$prefix" --disable-static --disable-crypto-dl \
        --with-libxml="$prefix" --with-libxslt="$prefix" --with-openssl \
        --without-gnutls --without-gcrypt --without-nss
    make -j2
    make install
)

# Retain explicit inventory for these source-built libraries; an OS package
# scanner alone does not establish coverage of this prefix.
mkdir -p "$prefix/share/posthog-native"
cat > "$prefix/share/posthog-native/components.json" <<'EOF'
{
  "components": [
    {"name": "libxml2", "version": "2.15.4", "source_sha256": "98087fd181d9070724f3fbc65c7377db03038eb92bd882374daff44940138821", "source": "https://download.gnome.org/sources/libxml2/2.15/libxml2-2.15.4.tar.xz"},
    {"name": "libxslt", "version": "1.1.45", "source_sha256": "9acfe68419c4d06a45c550321b3212762d92f41465062ca4ea19e632ee5d216e", "source": "https://download.gnome.org/sources/libxslt/1.1/libxslt-1.1.45.tar.xz"},
    {"name": "xmlsec1", "version": "1.3.12", "source_sha256": "24045199af12d93fe5fdbbbf7e386e823e4842071e9432e2b90ac108b889a923", "source": "https://github.com/lsh123/xmlsec/releases/download/1.3.12/xmlsec1-1.3.12.tar.gz"}
  ]
}
EOF
