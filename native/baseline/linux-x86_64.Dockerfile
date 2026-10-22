# This EOL image is an isolated ABI test fixture, not a recommended deployment
# or security-support promise. Signed Ubuntu archives remain required; no key,
# TLS or signature checks are disabled. No compiler/OpenMP library enters the
# final runtime layer. CPython is a pinned public checkout supplied by CI.
FROM ubuntu:21.04 AS python-build
RUN sed -i -e 's|http://archive.ubuntu.com/ubuntu|http://old-releases.ubuntu.com/ubuntu|g' \
           -e 's|http://security.ubuntu.com/ubuntu|http://old-releases.ubuntu.com/ubuntu|g' /etc/apt/sources.list \
    && apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       build-essential libffi-dev zlib1g-dev libbz2-dev liblzma-dev libreadline-dev libsqlite3-dev uuid-dev \
    && rm -rf /var/lib/apt/lists/*
COPY cpython /usr/src/cpython
WORKDIR /usr/src/cpython
RUN ./configure --prefix=/opt/python --with-ensurepip=install --without-static-libpython \
    && make -j4 && make install

FROM ubuntu:21.04
RUN sed -i -e 's|http://archive.ubuntu.com/ubuntu|http://old-releases.ubuntu.com/ubuntu|g' \
           -e 's|http://security.ubuntu.com/ubuntu|http://old-releases.ubuntu.com/ubuntu|g' /etc/apt/sources.list \
    && apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       libffi7 zlib1g libbz2-1.0 liblzma5 libreadline8 libsqlite3-0 libuuid1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=python-build /opt/python /opt/python
ENV PATH="/opt/python/bin:${PATH}" PYTHONDONTWRITEBYTECODE=1
RUN python3.10 -c "import ctypes, platform; assert platform.libc_ver() == ('glibc', '2.33')" \
    && ! command -v gcc && ! dpkg-query -W libgomp1
WORKDIR /work
